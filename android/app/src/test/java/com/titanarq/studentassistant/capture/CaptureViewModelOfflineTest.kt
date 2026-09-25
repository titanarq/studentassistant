@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.AudioFormat
import com.titanarq.studentassistant.protocol.Button
import com.titanarq.studentassistant.protocol.ButtonName
import com.titanarq.studentassistant.protocol.HelloAck
import com.titanarq.studentassistant.protocol.ServerAck
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionActiveStatus
import com.titanarq.studentassistant.protocol.SessionEndResponse
import com.titanarq.studentassistant.protocol.SessionEndedStatus
import com.titanarq.studentassistant.protocol.SttMode
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import com.titanarq.studentassistant.protocol.TranscriptFinal
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.session.SessionHolder
import com.titanarq.studentassistant.spool.PendingEnd
import com.titanarq.studentassistant.spool.SpoolBudget
import com.titanarq.studentassistant.spool.Spools
import java.io.File
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** The capture view model over the disk spool: offline, reconnect, end while offline, restart. */
class CaptureViewModelOfflineTest {
    @get:Rule
    val main = MainDispatcherRule(StandardTestDispatcher())

    @get:Rule
    val folder = TemporaryFolder()

    private val clock = FakeClock(2_000_000)
    private val backend = FakeBackendClient()
    private val sockets = FakeSessionSocketFactory()
    private var transcriber = FakeTranscriber()
    private val stillCapture = FakeStillCapture()
    private val holder = SessionHolder()
    private val credentials = BackendCredentials("http://192.168.1.20:8000", "sa_tok")
    private val session = Session(
        "s1", "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/s1", "1.1", listOf("c-0"),
    )
    private val open = OpenSession(credentials, session, "Historia", "El feudalismo").also(holder::open)
    private val ended = BackendResult.Success(SessionEndResponse("s1", SessionEndedStatus.ENDED, 9))
    private val root: File get() = File(folder.root, "spool")

    private fun TestScope.viewModel(spools: Spools): Pair<CaptureViewModel, SessionFinisher> {
        val uploads = CaptureUploadQueue(backgroundScope, backend, spool = spools.captures)
        val finisher = SessionFinisher(
            scope = backgroundScope,
            client = backend,
            spools = spools,
            uploads = uploads,
            socketFactory = sockets,
            clock = clock,
            credentials = { credentials },
            retryDelaysMs = listOf(1_000),
            reconnectDelaysMs = listOf(100),
        )
        val viewModel = CaptureViewModel(
            open = open,
            backendClient = backend,
            sessionHolder = holder,
            clock = clock,
            socketFactory = sockets,
            transcriberFactory = { transcriber },
            audioStreamerFactory = { error("server mode not used") },
            stillCapture = stillCapture,
            reconnectDelaysMs = listOf(100),
            spooling = CaptureSpooling(spools.audio("s1"), spools.events("s1"), spools.budget.nearCap, finisher),
        )
        return viewModel to finisher
    }

    private fun TestScope.handshake(mode: SttMode = SttMode.CLIENT) {
        sockets.last.open()
        runCurrent()
        sockets.last.receive(HelloAck("1.1", mode, if (mode == SttMode.SERVER) AudioFormat() else null, 0, clock.now))
        runCurrent()
    }

    private fun final(id: String) = ClientTranscript.Final(id, 10, 30, "línea $id", 0.8)

    private fun wire(id: String) = TranscriptClientFinal(id, 10, 30, "línea $id", "android-speech", "es-ES", 0.8)

    @Test
    fun `lines said while offline are resent on reconnect, even after the app process died`() = runTest(main.dispatcher) {
        val spools = Spools(root, SpoolBudget(10_000_000))
        val (viewModel, _) = viewModel(spools)
        viewModel.start()
        runCurrent()
        handshake()
        assertEquals(listOf(listOf("c-0")), stillCapture.resumes) // the start's received captures
        sockets.last.drop()
        runCurrent()
        transcriber.emit(final("a-0"))
        viewModel.important()
        runCurrent()
        viewModel.leave() // the process dies before the connection returns

        val restartedSpools = Spools(root, SpoolBudget(10_000_000))
        transcriber = FakeTranscriber()
        val (restarted, _) = viewModel(restartedSpools)
        restarted.start()
        runCurrent()
        handshake()
        val resent = sockets.last.sent.drop(1)
        assertEquals(wire("a-0"), resent[0])
        assertTrue(resent[1] is Button && (resent[1] as Button).button == ButtonName.IMPORTANT)
        sockets.last.receive(TranscriptFinal("a-0", 0, 1, "línea a-0", "es-ES"))
        runCurrent()
        assertEquals(emptyList<TranscriptClientFinal>(), restartedSpools.events("s1").finals())
        restarted.leave()
    }

    @Test
    fun `Terminar while offline ends the screen and the finisher completes it later`() = runTest(main.dispatcher) {
        val spools = Spools(root, SpoolBudget(10_000_000))
        val (viewModel, finisher) = viewModel(spools)
        viewModel.start()
        runCurrent()
        handshake()
        sockets.last.drop()
        runCurrent()
        transcriber.emit(final("a-1"))
        runCurrent()

        viewModel.end()
        runCurrent()
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertNull(holder.current.value)
        assertFalse(transcriber.running)
        assertEquals(listOf(PendingEnd("s1", credentials.baseUrl, clock.now)), spools.ends())
        assertEquals(setOf("s1"), finisher.pending.value)

        // The backend is back.
        backend.resumeSessionResult = BackendResult.Success(session)
        backend.endSessionResult = ended
        advanceTimeBy(1_000)
        runCurrent()
        handshake()
        assertEquals(wire("a-1"), sockets.last.sent[1])
        assertEquals(ButtonName.END_SESSION, (sockets.last.sent[2] as Button).button)
        assertTrue(backend.calls.none { it.startsWith("endSession") })
        sockets.last.receive(TranscriptFinal("a-1", 0, 1, "línea a-1", "es-ES"))
        runCurrent()
        assertEquals("endSession http://192.168.1.20:8000 s1", backend.calls.last())
        assertEquals(emptyList<PendingEnd>(), spools.ends())
        assertEquals(emptySet<String>(), finisher.pending.value)
    }

    @Test
    fun `an online Terminar waits for the last line, and a transient failure becomes a pending end`() = runTest(main.dispatcher) {
        val spools = Spools(root, SpoolBudget(10_000_000))
        val (viewModel, finisher) = viewModel(spools)
        backend.endSessionResult = BackendResult.Unreachable("timeout")
        viewModel.start()
        runCurrent()
        handshake()
        transcriber.emit(final("a-2"))
        runCurrent()
        viewModel.end()
        runCurrent()
        assertTrue(backend.calls.none { it.startsWith("endSession") }) // a-2 is not confirmed yet
        sockets.last.receive(TranscriptFinal("a-2", 0, 1, "línea a-2", "es-ES"))
        runCurrent()
        assertEquals(1, backend.calls.count { it.startsWith("endSession") })
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertEquals(setOf("s1"), finisher.pending.value)
        assertEquals(1, spools.ends().size)
    }

    @Test
    fun `Terminar y preparar apuntes while offline keeps the flag for the delivered end`() = runTest(main.dispatcher) {
        val spools = Spools(root, SpoolBudget(10_000_000))
        val (viewModel, finisher) = viewModel(spools)
        viewModel.start()
        runCurrent()
        handshake()
        sockets.last.drop()
        runCurrent()

        viewModel.end(prepareNotes = true)
        runCurrent()
        // A spooled end follows no progress: the screen ends as with a plain Terminar.
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertNull(holder.current.value)
        assertEquals(listOf(PendingEnd("s1", credentials.baseUrl, clock.now, prepareNotes = true)), spools.ends())

        backend.resumeSessionResult = BackendResult.HttpError(409)
        backend.endSessionResult = ended
        advanceTimeBy(1_000)
        runCurrent()
        assertEquals(listOf(true), backend.endSessionRequests.map { it.prepareNotes })
        assertEquals(emptySet<String>(), finisher.pending.value)
        assertTrue(backend.calls.none { it.startsWith("notesGeneration") })
    }

    @Test
    fun `a transient failure of Terminar y preparar apuntes becomes a pending end with the flag`() = runTest(main.dispatcher) {
        val spools = Spools(root, SpoolBudget(10_000_000))
        val (viewModel, _) = viewModel(spools)
        backend.endSessionResult = BackendResult.HttpError(503)
        viewModel.start()
        runCurrent()
        handshake()
        viewModel.end(prepareNotes = true)
        runCurrent()
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertEquals(listOf(true), spools.ends().map { it.prepareNotes })
    }

    @Test
    fun `an online Terminar that succeeds deletes the session's spool`() = runTest(main.dispatcher) {
        val spools = Spools(root, SpoolBudget(10_000_000))
        val (viewModel, _) = viewModel(spools)
        backend.endSessionResult = ended
        viewModel.start()
        runCurrent()
        handshake()
        viewModel.end()
        runCurrent()
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertEquals(emptyList<String>(), spools.sessionIds())
        assertEquals(emptyList<PendingEnd>(), spools.ends())
    }

    @Test
    fun `the near-cap warning follows the spool and acked captures are confirmed`() = runTest(main.dispatcher) {
        val spools = Spools(root, SpoolBudget(1_000, warnFraction = 0.5))
        val (viewModel, _) = viewModel(spools)
        viewModel.start()
        runCurrent()
        handshake()
        assertFalse(viewModel.state.value.spoolNearCap)
        spools.budget.add(600)
        runCurrent()
        assertTrue(viewModel.state.value.spoolNearCap)
        spools.budget.add(-600)
        runCurrent()
        assertFalse(viewModel.state.value.spoolNearCap)

        sockets.last.receive(ServerAck(captureIds = listOf("c-7"), serverTimeMs = 1))
        runCurrent()
        assertEquals(listOf(listOf("c-7")), stillCapture.confirmed)
        viewModel.leave()
    }
}
