@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import kotlinx.coroutines.ExperimentalCoroutinesApi
import com.titanarq.studentassistant.protocol.AudioFormat
import com.titanarq.studentassistant.protocol.Button
import com.titanarq.studentassistant.protocol.ButtonName
import com.titanarq.studentassistant.protocol.ClientCapabilities
import com.titanarq.studentassistant.protocol.Hello
import com.titanarq.studentassistant.protocol.HelloAck
import com.titanarq.studentassistant.protocol.Notice
import com.titanarq.studentassistant.protocol.ServerAck
import com.titanarq.studentassistant.protocol.ServerEvent
import com.titanarq.studentassistant.protocol.SttMode
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import com.titanarq.studentassistant.protocol.TranscriptClientPartial
import com.titanarq.studentassistant.protocol.TranscriptFinal
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SessionConnectionTest {
    private val clock = FakeClock()
    private val sockets = FakeSessionSocketFactory()
    private val capabilities = ClientCapabilities(SttMode.CLIENT, "android-speech", AudioFormat())
    private var resumes = 0
    private var resumeAnswer = true

    private fun TestScope.connection(scope: CoroutineScope = backgroundScope) = SessionConnection(
        scope = scope,
        socketFactory = sockets,
        url = "http://192.168.1.20:8000/ws/sessions/s1",
        token = "sa_tok",
        clock = clock,
        capabilities = capabilities,
        resume = {
            resumes++
            resumeAnswer
        },
        reconnectDelaysMs = listOf(100, 1_000),
    )

    private fun ack(mode: SttMode, offset: Long = 5) = HelloAck(
        protocolVersion = "1.1",
        sttMode = mode,
        audioFormat = if (mode == SttMode.SERVER) AudioFormat() else null,
        clockOffsetMs = offset,
        serverTimeMs = clock.now + offset,
    )

    private fun final(id: String) = TranscriptClientFinal(id, 1, 2, "hola $id", "android-speech", "es-ES")

    private fun echo(id: String) = TranscriptFinal(id, 0, 1, "hola $id", "es-ES")

    /** Runs everything due within the longest back-off (backgroundScope work is not "idle"-tracked). */
    private fun TestScope.settle() {
        advanceTimeBy(1_000)
        runCurrent()
    }

    /** Opens the current socket and completes the handshake in [mode]. */
    private fun TestScope.handshake(mode: SttMode) {
        sockets.last.open()
        runCurrent()
        sockets.last.receive(ack(mode))
        runCurrent()
    }

    @Test
    fun `hello goes first and events queued before hello_ack follow it`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        val socket = sockets.last
        assertEquals("http://192.168.1.20:8000/ws/sessions/s1", socket.url)
        assertEquals("sa_tok", socket.token)
        assertEquals(ConnectionState.Connecting, connection.state.value)

        connection.send(Button(ButtonName.IMPORTANT, null, 7))
        socket.open()
        runCurrent()
        assertEquals(listOf(Hello("1.1", capabilities, clock.now)), socket.sent)

        socket.receive(ack(SttMode.CLIENT, offset = 42))
        runCurrent()
        assertEquals(ConnectionState.Connected(SttMode.CLIENT, 42), connection.state.value)
        assertEquals(Button(ButtonName.IMPORTANT, null, 7), socket.sent.last())
    }

    @Test
    fun `server events after the handshake reach the events flow`() = runTest {
        val connection = connection()
        val received = mutableListOf<ServerEvent>()
        backgroundScope.launch { connection.events.collect { received += it } }
        connection.start()
        runCurrent()
        handshake(SttMode.CLIENT)
        sockets.last.receive(Notice(3, 10))
        runCurrent()
        assertEquals(listOf(ack(SttMode.CLIENT), Notice(3, 10)), received)
    }

    @Test
    fun `unechoed finals are resent after a reconnect and partials are dropped while offline`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        handshake(SttMode.CLIENT)
        connection.send(final("a-0"))
        connection.send(final("a-1"))
        runCurrent()
        sockets.last.receive(echo("a-0")) // the backend confirmed a-0 only
        runCurrent()

        sockets.last.drop()
        runCurrent()
        assertEquals(ConnectionState.Reconnecting(1, "connection reset"), connection.state.value)
        connection.send(TranscriptClientPartial("a-2", 3, 4, "y lue", "android-speech", "es-ES"))
        connection.send(final("a-2"))
        connection.send(Button(ButtonName.IMPORTANT, null, 9))
        runCurrent()
        assertEquals(1, sockets.sockets.size)

        advanceTimeBy(99)
        runCurrent()
        assertEquals(1, sockets.sockets.size)
        advanceTimeBy(1)
        runCurrent()
        assertEquals(2, sockets.sockets.size)
        handshake(SttMode.CLIENT)
        val resent = sockets.last.sent.drop(1) // after hello
        assertEquals(listOf(final("a-1"), final("a-2"), Button(ButtonName.IMPORTANT, null, 9)), resent)
    }

    @Test
    fun `the back-off grows and resets after a handshake`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        sockets.last.drop()
        runCurrent()
        advanceTimeBy(100)
        runCurrent()
        assertEquals(2, sockets.sockets.size)
        sockets.last.drop()
        runCurrent()
        assertEquals(ConnectionState.Reconnecting(2, "connection reset"), connection.state.value)
        advanceTimeBy(999)
        runCurrent()
        assertEquals(2, sockets.sockets.size)
        advanceTimeBy(1)
        runCurrent()
        assertEquals(3, sockets.sockets.size)
        handshake(SttMode.CLIENT)
        sockets.last.drop()
        runCurrent()
        assertEquals(ConnectionState.Reconnecting(1, "connection reset"), connection.state.value)
    }

    @Test
    fun `audio frames are numbered and resent from the last acked seq`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        handshake(SttMode.SERVER)
        repeat(3) { i -> connection.sendAudio(shortArrayOf(i.toShort(), 1), 5_000L + i * 100) }
        runCurrent()
        assertEquals(listOf(0L, 1L, 2L), sockets.last.frames.map { it.seq })
        assertEquals(listOf(5_000L, 5_100L, 5_200L), sockets.last.frames.map { it.clientTimeMs })
        assertEquals(listOf<Short>(2, 1), sockets.last.frames.last().samples.toList())

        sockets.last.receive(ServerAck(audioSeq = 1, serverTimeMs = 1))
        runCurrent()
        sockets.last.drop()
        runCurrent()
        connection.sendAudio(shortArrayOf(3), 5_300) // captured while offline
        settle()
        handshake(SttMode.SERVER)
        assertEquals(listOf(2L, 3L), sockets.last.frames.map { it.seq })
    }

    @Test
    fun `frames already covered by the backend's ack are renumbered after it`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        handshake(SttMode.SERVER)
        connection.sendAudio(shortArrayOf(1), 10)
        connection.sendAudio(shortArrayOf(2), 110)
        runCurrent()
        // A restarted app on a resumed session: the backend holds frames up to 41 already.
        sockets.last.receive(ServerAck(audioSeq = 41, serverTimeMs = 1))
        runCurrent()
        connection.sendAudio(shortArrayOf(3), 210)
        runCurrent()
        assertEquals(listOf(0L, 1L, 42L, 43L, 44L), sockets.last.frames.map { it.seq })
        assertEquals(listOf(10L, 110L, 10L, 110L, 210L), sockets.last.frames.map { it.clientTimeMs })
    }

    @Test
    fun `no audio is sent in client mode and no transcript in server mode`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        handshake(SttMode.CLIENT)
        connection.sendAudio(shortArrayOf(1), 10)
        runCurrent()
        assertTrue(sockets.last.binaries.isEmpty())

        sockets.last.drop()
        settle()
        handshake(SttMode.SERVER)
        connection.send(final("a-0"))
        runCurrent()
        assertEquals(1, sockets.last.texts.size) // hello only
    }

    @Test
    fun `a session that is not active is resumed and reconnected`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        sockets.last.closeByServer(4404, "session s1 is not active: start or resume it first")
        runCurrent()
        assertEquals(1, resumes)
        assertEquals(2, sockets.sockets.size)

        resumeAnswer = false
        sockets.last.closeByServer(4404, "not active")
        runCurrent()
        assertEquals(ConnectionState.Failed(ConnectionFailure.SessionNotActive), connection.state.value)
        assertEquals(2, sockets.sockets.size)

        connection.retry()
        runCurrent()
        assertEquals(3, sockets.sockets.size)
    }

    @Test
    fun `a refused handshake or a contract violation fails without retrying`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        sockets.last.drop("HTTP 403")
        settle()
        assertEquals(ConnectionState.Failed(ConnectionFailure.Unauthorized("HTTP 403")), connection.state.value)
        assertEquals(1, sockets.sockets.size)

        connection.retry()
        runCurrent()
        sockets.last.open()
        runCurrent()
        sockets.last.closeByServer(1008, "incompatible protocol_version 2.0")
        settle()
        assertEquals(
            ConnectionState.Failed(ConnectionFailure.Refused("incompatible protocol_version 2.0")),
            connection.state.value,
        )
        assertEquals(2, sockets.sockets.size)
    }

    @Test
    fun `an invalid server message closes the socket as refused`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        sockets.last.open()
        runCurrent()
        sockets.last.receive(Notice(1, 1)) // not hello.ack
        runCurrent()
        assertTrue(sockets.last.closed)
        assertTrue(connection.state.value is ConnectionState.Failed)

        connection.retry()
        runCurrent()
        handshake(SttMode.CLIENT)
        sockets.last.receiveText("""{"type":"mystery"}""")
        runCurrent()
        assertTrue(sockets.last.closed)
        assertTrue(connection.state.value is ConnectionState.Failed)
    }

    @Test
    fun `stop closes the socket and ignores everything after`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        handshake(SttMode.CLIENT)
        val socket = sockets.last
        connection.stop()
        runCurrent()
        assertTrue(socket.closed)
        assertEquals(ConnectionState.Stopped, connection.state.value)
        connection.send(Button(ButtonName.IMPORTANT, null, 1))
        socket.drop()
        settle()
        assertEquals(1, sockets.sockets.size)
        assertEquals(ConnectionState.Stopped, connection.state.value)
    }

    @Test
    fun `the socket url joins the base url and ws_path`() {
        assertEquals(
            "http://pc:8000/ws/sessions/s1",
            SessionConnection.socketUrl("http://pc:8000/", "/ws/sessions/s1"),
        )
    }
}
