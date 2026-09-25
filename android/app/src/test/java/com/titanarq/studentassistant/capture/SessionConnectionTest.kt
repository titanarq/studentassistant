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
import com.titanarq.studentassistant.spool.AudioBacklog
import com.titanarq.studentassistant.spool.AudioSpool
import com.titanarq.studentassistant.spool.EventBacklog
import com.titanarq.studentassistant.spool.EventSpool
import com.titanarq.studentassistant.spool.MemoryAudioBacklog
import com.titanarq.studentassistant.spool.MemoryEventBacklog
import com.titanarq.studentassistant.spool.SpoolBudget
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class SessionConnectionTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val clock = FakeClock()
    private val sockets = FakeSessionSocketFactory()
    private val capabilities = ClientCapabilities(SttMode.CLIENT, "android-speech", AudioFormat())
    private var resumes = 0
    private var resumeAnswer = true

    private fun TestScope.connection(
        scope: CoroutineScope = backgroundScope,
        audio: AudioBacklog = MemoryAudioBacklog(SessionConnection.MAX_BUFFERED_FRAMES),
        backlog: EventBacklog = MemoryEventBacklog(),
    ) = SessionConnection(
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
        audio = audio,
        backlog = backlog,
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
        assertEquals(listOf(Hello("1.3", capabilities, clock.now)), socket.sent)

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
        assertTrue(sockets.last.frames.isEmpty()) // waiting for the backend's ack
        sockets.last.receive(ServerAck(audioSeq = 1, serverTimeMs = 2))
        runCurrent()
        assertEquals(listOf(2L, 3L), sockets.last.frames.map { it.seq })
        connection.sendAudio(shortArrayOf(4), 5_400) // live again
        runCurrent()
        assertEquals(listOf(2L, 3L, 4L), sockets.last.frames.map { it.seq })
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
    fun `without an ack after hello_ack the held audio is resent once the wait is over`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        sockets.last.drop() // offline from the start
        connection.sendAudio(shortArrayOf(1), 10)
        connection.sendAudio(shortArrayOf(2), 110)
        settle()
        handshake(SttMode.SERVER)
        assertTrue(sockets.last.frames.isEmpty())
        advanceTimeBy(SessionConnection.ACK_WAIT_MS)
        runCurrent()
        assertEquals(listOf(0L, 1L), sockets.last.frames.map { it.seq })
    }

    @Test
    fun `held frames that do not follow the backend's ack are renumbered after it`() = runTest {
        // Frames 0..4 were dropped at the spool's cap; the backend holds up to 1 only.
        val audio = MemoryAudioBacklog(maxFrames = 3)
        val connection = connection(audio = audio)
        connection.start()
        runCurrent()
        sockets.last.drop()
        repeat(6) { i -> connection.sendAudio(shortArrayOf(i.toShort()), 1_000L + i) }
        settle()
        handshake(SttMode.SERVER)
        sockets.last.receive(ServerAck(audioSeq = 1, serverTimeMs = 2))
        runCurrent()
        assertEquals(listOf(2L, 3L, 4L), sockets.last.frames.map { it.seq })
        assertEquals(listOf(1_003L, 1_004L, 1_005L), sockets.last.frames.map { it.clientTimeMs })
        connection.sendAudio(shortArrayOf(9), 2_000)
        runCurrent()
        assertEquals(5L, sockets.last.frames.last().seq)
    }

    @Test
    fun `resending pauses while the socket's send queue is full`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        sockets.last.drop()
        repeat(50) { i -> connection.sendAudio(shortArrayOf(i.toShort()), 1_000L + i) }
        settle()
        handshake(SttMode.SERVER)
        val socket = sockets.last
        socket.queued = SessionConnection.MAX_SOCKET_QUEUE_BYTES + 1
        socket.receive(ServerAck(audioSeq = 9, serverTimeMs = 2))
        runCurrent()
        assertTrue(socket.frames.isEmpty())
        connection.sendAudio(shortArrayOf(99), 5_000) // live, behind the backlog
        runCurrent()
        assertTrue(socket.frames.isEmpty())
        socket.queued = 0
        advanceTimeBy(100)
        runCurrent()
        assertEquals((10L..50L).toList(), socket.frames.map { it.seq })
        assertEquals(5_000L, socket.frames.last().clientTimeMs)
    }

    @Test
    fun `a resent final the backend never echoes is settled after the grace time, then it is drained`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        handshake(SttMode.CLIENT)
        assertTrue(connection.drained.value)
        connection.send(final("a-0"))
        runCurrent()
        assertFalse(connection.drained.value)
        sockets.last.drop() // the echo was lost
        settle()
        handshake(SttMode.CLIENT)
        assertEquals(listOf(final("a-0")), sockets.last.sent.drop(1))
        assertFalse(connection.drained.value)
        advanceTimeBy(SessionConnection.FINAL_GRACE_MS)
        runCurrent()
        assertTrue(connection.drained.value)
        sockets.last.drop()
        settle()
        handshake(SttMode.CLIENT)
        assertEquals(1, sockets.last.sent.size) // hello only
    }

    @Test
    fun `a disk backlog survives a new connection, as after an app restart`() = runTest {
        val budget = SpoolBudget(10_000_000)
        val audioDir = folder.newFolder("audio")
        val eventsFile = folder.root.resolve("events.json")
        val first = connection(audio = AudioSpool(audioDir, budget), backlog = EventSpool(eventsFile))
        first.start()
        runCurrent()
        handshake(SttMode.SERVER)
        repeat(3) { i -> first.sendAudio(shortArrayOf(i.toShort()), 7_000L + i * 100) }
        first.send(Button(ButtonName.IMPORTANT, null, 1))
        runCurrent()
        sockets.last.receive(ServerAck(audioSeq = 0, serverTimeMs = 2))
        runCurrent()
        sockets.last.drop()
        first.sendAudio(shortArrayOf(3), 7_300) // offline
        first.send(Button(ButtonName.SWITCH_SOURCE, com.titanarq.studentassistant.protocol.SourceKind.BOOK, 2))
        runCurrent()
        first.stop() // the process dies
        runCurrent()

        val second = connection(audio = AudioSpool(audioDir, budget), backlog = EventSpool(eventsFile))
        second.start()
        runCurrent()
        handshake(SttMode.SERVER)
        assertEquals(
            listOf(Button(ButtonName.SWITCH_SOURCE, com.titanarq.studentassistant.protocol.SourceKind.BOOK, 2)),
            sockets.last.sent.drop(1),
        )
        sockets.last.receive(ServerAck(audioSeq = 1, serverTimeMs = 3))
        runCurrent()
        assertEquals(listOf(2L, 3L), sockets.last.frames.map { it.seq })
        assertEquals(listOf(7_200L, 7_300L), sockets.last.frames.map { it.clientTimeMs })
        second.sendAudio(shortArrayOf(4), 7_400)
        runCurrent()
        assertEquals(4L, sockets.last.frames.last().seq)
        sockets.last.receive(ServerAck(audioSeq = 4, serverTimeMs = 4))
        runCurrent()
        assertTrue(second.drained.value)
    }

    @Test
    fun `resent finals survive a reconnect whose link dies unnoticed before the grace time`() = runTest {
        val connection = connection()
        connection.start()
        runCurrent()
        handshake(SttMode.CLIENT)
        connection.send(final("a-0"))
        connection.send(final("a-1"))
        runCurrent()
        sockets.last.drop()
        settle()
        handshake(SttMode.CLIENT) // resends a-0, a-1 ...
        assertEquals(listOf(final("a-0"), final("a-1")), sockets.last.sent.drop(1))
        // ... but the link is already dead: silent, and noticed only by the pings (~20 s).
        advanceTimeBy(20_000)
        runCurrent()
        assertFalse(connection.drained.value)
        sockets.last.drop("ping timeout")
        settle()
        handshake(SttMode.CLIENT)
        assertEquals(listOf(final("a-0"), final("a-1")), sockets.last.sent.drop(1)) // nothing lost
        sockets.last.receive(echo("a-0"))
        runCurrent()
        advanceTimeBy(SessionConnection.FINAL_GRACE_MS) // a-1 never echoed on a live link: held
        runCurrent()
        assertTrue(connection.drained.value)
    }

    @Test
    fun `without the backend's ack in time frames go out as numbered and are never renumbered later`() = runTest {
        val audio = MemoryAudioBacklog(maxFrames = 3)
        val connection = connection(audio = audio)
        connection.start()
        runCurrent()
        handshake(SttMode.SERVER)
        repeat(3) { i -> connection.sendAudio(shortArrayOf(i.toShort()), 1_000L + i) }
        runCurrent()
        sockets.last.receive(ServerAck(audioSeq = 0, serverTimeMs = 1))
        runCurrent()
        sockets.last.drop()
        repeat(6) { i -> connection.sendAudio(shortArrayOf(i.toShort()), 2_000L + i) } // 3..8; 1..5 dropped
        settle()
        handshake(SttMode.SERVER)
        advanceTimeBy(SessionConnection.ACK_WAIT_MS) // the link dies: no ack
        runCurrent()
        assertEquals(listOf(6L, 7L, 8L), sockets.last.frames.map { it.seq })
        sockets.last.receive(ServerAck(audioSeq = 2, serverTimeMs = 2)) // late: frames past it were sent
        runCurrent()
        assertEquals(listOf(6L, 7L, 8L), sockets.last.frames.map { it.seq })
        assertEquals(listOf(6L, 7L, 8L), audio.after(-1, 10).map { it.seq })
    }

    @Test
    fun `a backlog that never saw an ack and lost its first frames starts again from 0`() = runTest {
        val connection = connection(audio = MemoryAudioBacklog(maxFrames = 2))
        connection.start()
        runCurrent()
        sockets.last.drop()
        repeat(4) { i -> connection.sendAudio(shortArrayOf(i.toShort()), 1_000L + i) } // 0..1 dropped
        settle()
        handshake(SttMode.SERVER)
        advanceTimeBy(SessionConnection.ACK_WAIT_MS) // the backend holds no audio: it sends no ack
        runCurrent()
        assertEquals(listOf(0L, 1L), sockets.last.frames.map { it.seq })
        assertEquals(listOf(1_002L, 1_003L), sockets.last.frames.map { it.clientTimeMs })
    }

    @Test
    fun `the socket url joins the base url and ws_path`() {
        assertEquals(
            "http://pc:8000/ws/sessions/s1",
            SessionConnection.socketUrl("http://pc:8000/", "/ws/sessions/s1"),
        )
    }
}
