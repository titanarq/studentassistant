@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import kotlinx.coroutines.ExperimentalCoroutinesApi
import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.AudioFormat
import com.titanarq.studentassistant.protocol.Button
import com.titanarq.studentassistant.protocol.ButtonName
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.ClientAck
import com.titanarq.studentassistant.protocol.ClientCapabilities
import com.titanarq.studentassistant.protocol.Command
import com.titanarq.studentassistant.protocol.CommandName
import com.titanarq.studentassistant.protocol.Hello
import com.titanarq.studentassistant.protocol.HelloAck
import com.titanarq.studentassistant.protocol.Notice
import com.titanarq.studentassistant.protocol.NotesGenerationStart
import com.titanarq.studentassistant.protocol.NotesGenerationState
import com.titanarq.studentassistant.protocol.NotesGenerationStatus
import com.titanarq.studentassistant.desk.DeskTopic
import com.titanarq.studentassistant.protocol.PROTOCOL_VERSION
import com.titanarq.studentassistant.protocol.SttState
import com.titanarq.studentassistant.protocol.SttStatus
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionActiveStatus
import com.titanarq.studentassistant.protocol.SessionEndResponse
import com.titanarq.studentassistant.protocol.SessionEndedStatus
import com.titanarq.studentassistant.protocol.SourceKind
import com.titanarq.studentassistant.protocol.SttMode
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import com.titanarq.studentassistant.protocol.TranscriptClientPartial
import com.titanarq.studentassistant.protocol.TranscriptFinal
import com.titanarq.studentassistant.protocol.TranscriptPartial
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.session.SessionHolder
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.launch
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class CaptureViewModelTest {
    @get:Rule
    val main = MainDispatcherRule(StandardTestDispatcher())

    private val clock = FakeClock(2_000_000)
    private val backend = FakeBackendClient()
    private val sockets = FakeSessionSocketFactory()
    private val transcriber = FakeTranscriber()
    private val stillCapture = FakeStillCapture()
    private val captures get() = stillCapture.captures
    private val holder = SessionHolder()
    private var audioSource = FakeAudioSource(totalSamples = 1600 * 2)
    private val open = OpenSession(
        backend = BackendCredentials("http://192.168.1.20:8000", "sa_tok"),
        session = Session("s1", "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/s1", "1.1"),
        subjectName = "Historia",
        topicName = "El feudalismo",
    ).also(holder::open)

    private fun viewModel() = CaptureViewModel(
        open = open,
        backendClient = backend,
        sessionHolder = holder,
        clock = clock,
        socketFactory = sockets,
        transcriberFactory = { transcriber },
        audioStreamerFactory = { AudioStreamer(audioSource, clock, main.dispatcher) },
        stillCapture = stillCapture,
        reconnectDelaysMs = listOf(100),
        notesPollIntervalMs = POLL_MS,
    )

    private fun TestScope.connect(viewModel: CaptureViewModel, mode: SttMode = SttMode.CLIENT) {
        viewModel.start()
        runCurrent()
        sockets.last.open()
        runCurrent()
        sockets.last.receive(
            HelloAck("1.1", mode, if (mode == SttMode.SERVER) AudioFormat() else null, 0, clock.now),
        )
        runCurrent()
    }

    @Test
    fun `start opens the session socket and, in client mode, the transcriber`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        assertEquals(CapturePhase.IDLE, viewModel.state.value.phase)
        connect(viewModel)

        val socket = sockets.last
        assertEquals("http://192.168.1.20:8000/ws/sessions/s1", socket.url)
        assertEquals("sa_tok", socket.token)
        assertEquals(
            Hello(PROTOCOL_VERSION, ClientCapabilities(SttMode.CLIENT, "android-speech", AudioFormat()), clock.now),
            socket.sent.first(),
        )
        assertEquals(CapturePhase.RUNNING, viewModel.state.value.phase)
        assertEquals(ConnectionState.Connected(SttMode.CLIENT, 0), viewModel.state.value.connection)
        assertTrue(transcriber.running)

        transcriber.emit(ClientTranscript.Partial("and-x-0", 10, 20, "el feu"))
        transcriber.emit(ClientTranscript.Final("and-x-0", 10, 30, "El feudalismo", 0.8))
        runCurrent()
        assertEquals(
            listOf(
                TranscriptClientPartial("and-x-0", 10, 20, "el feu", "android-speech", "es-ES"),
                TranscriptClientFinal("and-x-0", 10, 30, "El feudalismo", "android-speech", "es-ES", 0.8),
            ),
            socket.sent.drop(1),
        )
        viewModel.leave()
    }

    @Test
    fun `the transcript and the pending counter follow the server events`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel)
        val socket = sockets.last
        socket.receive(TranscriptPartial("a-0", 0, 100, "el feu", "es-ES"))
        runCurrent()
        assertEquals(listOf(TranscriptLine("a-0", "el feu", final = false)), viewModel.state.value.transcript)

        socket.receive(TranscriptFinal("a-0", 0, 900, "El feudalismo", "es-ES"))
        socket.receive(TranscriptPartial("a-0", 0, 100, "el feu", "es-ES")) // late: ignored
        socket.receive(TranscriptPartial("a-1", 1_000, 1_100, "era", "es-ES"))
        socket.receive(Notice(pendingCount = 2, serverTimeMs = 5))
        runCurrent()
        assertEquals(
            listOf(TranscriptLine("a-0", "El feudalismo", final = true), TranscriptLine("a-1", "era", final = false)),
            viewModel.state.value.transcript,
        )
        assertEquals(2, viewModel.state.value.pendingCount)
        viewModel.leave()
    }

    @Test
    fun `the buttons send their protocol events and Capturar hands over to still capture`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel)
        val socket = sockets.last
        clock.now = 2_000_100
        viewModel.important()
        viewModel.toggleSource()
        assertEquals(SourceKind.BOOK, viewModel.state.value.source)
        viewModel.toggleSource()
        viewModel.capture()
        runCurrent()
        assertEquals(
            listOf(
                Button(ButtonName.IMPORTANT, null, 2_000_100),
                Button(ButtonName.SWITCH_SOURCE, SourceKind.BOOK, 2_000_100),
                Button(ButtonName.SWITCH_SOURCE, SourceKind.NOTES, 2_000_100),
            ),
            socket.sent.drop(1),
        )
        assertEquals(listOf(CaptureTrigger.BUTTON to null), captures)
        viewModel.leave()
    }

    @Test
    fun `capture_now asks still capture and is acknowledged`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel)
        sockets.last.receive(Command("cmd-1", CommandName.CAPTURE_NOW, 5))
        runCurrent()
        assertEquals(listOf(CaptureTrigger.COMMAND to "cmd-1"), captures)
        assertEquals(ClientAck("cmd-1", clock.now), sockets.last.sent.last())
        viewModel.leave()
    }

    @Test
    fun `the thumbnail strip follows still capture and a tap retries a failed shot`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        val collector = backgroundScope.launch { viewModel.shots.collect {} }
        val shot = CaptureShot("c-1", "s1", CaptureTrigger.BUTTON, 5, ShotStatus.FAILED)
        stillCapture.shots.value = listOf(shot)
        runCurrent()
        assertEquals(listOf(shot), viewModel.shots.value)
        viewModel.retryShot("c-1")
        assertEquals(listOf("c-1"), stillCapture.retries)
        collector.cancel()
    }

    @Test
    fun `Terminar sends end_session, ends it over REST and clears the session`() = runTest(main.dispatcher) {
        backend.endSessionResult = BackendResult.Success(SessionEndResponse("s1", SessionEndedStatus.ENDED, 9))
        val viewModel = viewModel()
        connect(viewModel)
        val socket = sockets.last
        viewModel.end()
        advanceUntilIdle()

        assertEquals(Button(ButtonName.END_SESSION, null, clock.now), socket.sent.last())
        assertEquals(listOf("endSession http://192.168.1.20:8000 s1"), backend.calls)
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertNull(holder.current.value)
        assertTrue(socket.closed)
        assertFalse(transcriber.running)
    }

    @Test
    fun `a failed Terminar keeps the session running, an already ended one counts as ended`() = runTest(main.dispatcher) {
        backend.endSessionResult = BackendResult.Unreachable("down")
        val viewModel = viewModel()
        connect(viewModel)
        viewModel.end()
        advanceUntilIdle()
        assertEquals(CapturePhase.RUNNING, viewModel.state.value.phase)
        assertEquals(BackendResult.Unreachable("down"), viewModel.state.value.endFailure)
        assertNotNull(holder.current.value)
        assertTrue(transcriber.running)

        backend.endSessionResult = BackendResult.HttpError(409)
        viewModel.end()
        advanceUntilIdle()
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertNull(holder.current.value)
    }

    @Test
    fun `plain Terminar sends no prepare_notes and polls nothing`() = runTest(main.dispatcher) {
        backend.endSessionResult = BackendResult.Success(SessionEndResponse("s1", SessionEndedStatus.ENDED, 1))
        val viewModel = viewModel()
        connect(viewModel)
        viewModel.end()
        advanceTimeBy(POLL_MS * 3)
        runCurrent()
        assertEquals(listOf(null), backend.endSessionRequests.map { it.prepareNotes })
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertNull(viewModel.state.value.notesProgress)
        assertTrue(backend.calls.none { it.startsWith("notesGeneration") })
    }

    private fun prepared(start: NotesGenerationStart?) =
        BackendResult.Success(SessionEndResponse("s1", SessionEndedStatus.ENDED, 1, start))

    private fun status(state: NotesGenerationState, detail: String? = null, version: Int? = null, draft: Boolean? = null) =
        BackendResult.Success(
            NotesGenerationStatus("historia", "feudalismo", state, version = version, draft = draft, detail = detail),
        )

    private fun pollCalls() = backend.calls.count { it.startsWith("notesGeneration") }

    @Test
    fun `Terminar y preparar apuntes sends the flag and follows the generation until it is done`() = runTest(main.dispatcher) {
        backend.endSessionResult = prepared(NotesGenerationStart.STARTED)
        backend.notesGenerationResults += status(NotesGenerationState.RUNNING)
        backend.notesGenerationResults += BackendResult.Unreachable("wifi")
        backend.notesGenerationResult = status(NotesGenerationState.DONE, version = 2, draft = false)
        val viewModel = viewModel()
        connect(viewModel)
        val socket = sockets.last

        viewModel.end(prepareNotes = true)
        runCurrent()
        assertEquals(listOf(true), backend.endSessionRequests.map { it.prepareNotes })
        assertEquals(CapturePhase.NOTES, viewModel.state.value.phase)
        assertEquals(NotesProgress.Running(), viewModel.state.value.notesProgress)
        assertTrue(socket.closed)
        assertFalse(transcriber.running)
        assertNotNull(holder.current.value) // kept until the student leaves the progress
        assertEquals(0, pollCalls())

        advanceTimeBy(POLL_MS)
        runCurrent()
        assertEquals("notesGeneration http://192.168.1.20:8000 historia/feudalismo", backend.calls.last())
        assertEquals(NotesProgress.Running(), viewModel.state.value.notesProgress)
        advanceTimeBy(POLL_MS)
        runCurrent()
        assertEquals(NotesProgress.Running(BackendResult.Unreachable("wifi")), viewModel.state.value.notesProgress)
        advanceTimeBy(POLL_MS)
        runCurrent()
        assertEquals(NotesProgress.Done(2, false, null), viewModel.state.value.notesProgress)

        // Done: polling stops.
        advanceTimeBy(POLL_MS * 5)
        runCurrent()
        assertEquals(3, pollCalls())
        assertEquals(DeskTopic("historia", "feudalismo", "El feudalismo"), viewModel.deskTopic)

        viewModel.closeNotes()
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertNull(holder.current.value)
    }

    @Test
    fun `a failed, capped, forgotten or refused generation is shown and stops the polling`() = runTest(main.dispatcher) {
        val cases = listOf(
            status(NotesGenerationState.FAILED, detail = "Claude no respondió") to NotesProgress.Failed("Claude no respondió"),
            status(NotesGenerationState.NEEDS_CONFIRMATION, detail = "Límite") to NotesProgress.NeedsConfirmation("Límite"),
            status(NotesGenerationState.IDLE) to NotesProgress.Lost,
            BackendResult.HttpError(404) to NotesProgress.Unknown(BackendResult.HttpError(404)),
        )
        for ((answer, expected) in cases) {
            backend.calls.clear()
            backend.endSessionResult = prepared(NotesGenerationStart.RUNNING)
            backend.notesGenerationResult = answer
            val viewModel = viewModel()
            connect(viewModel)
            viewModel.end(prepareNotes = true)
            runCurrent()
            advanceTimeBy(POLL_MS * 4)
            runCurrent()
            assertEquals(expected, viewModel.state.value.notesProgress)
            assertEquals(1, pollCalls())
            assertEquals(CapturePhase.NOTES, viewModel.state.value.phase)
            viewModel.leave() // back: the same as "Volver al inicio"
            assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
            holder.open(open)
        }
    }

    @Test
    fun `a backend that cannot prepare notes is said at once, with no polling`() = runTest(main.dispatcher) {
        for (start in listOf(NotesGenerationStart.UNAVAILABLE, null)) {
            backend.calls.clear()
            backend.endSessionResult = prepared(start)
            val viewModel = viewModel()
            connect(viewModel)
            viewModel.end(prepareNotes = true)
            advanceTimeBy(POLL_MS * 3)
            runCurrent()
            assertEquals(CapturePhase.NOTES, viewModel.state.value.phase)
            assertEquals(NotesProgress.Unavailable, viewModel.state.value.notesProgress)
            assertEquals(0, pollCalls())
            viewModel.closeNotes()
            holder.open(open)
        }
    }

    @Test
    fun `an already ended session follows no generation`() = runTest(main.dispatcher) {
        backend.endSessionResult = BackendResult.HttpError(409)
        val viewModel = viewModel()
        connect(viewModel)
        viewModel.end(prepareNotes = true)
        advanceTimeBy(POLL_MS * 3)
        runCurrent()
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
        assertNull(holder.current.value)
        assertEquals(0, pollCalls())
    }

    @Test
    fun `polling pauses in the background and stops when the screen is left`() = runTest(main.dispatcher) {
        backend.endSessionResult = prepared(NotesGenerationStart.STARTED)
        backend.notesGenerationResult = status(NotesGenerationState.RUNNING)
        val viewModel = viewModel()
        connect(viewModel)
        viewModel.end(prepareNotes = true)
        runCurrent()
        advanceTimeBy(POLL_MS)
        runCurrent()
        assertEquals(1, pollCalls())

        viewModel.onBackground()
        advanceTimeBy(POLL_MS * 5)
        runCurrent()
        assertEquals(1, pollCalls())
        assertFalse(viewModel.state.value.micPaused)

        viewModel.onForeground()
        advanceTimeBy(POLL_MS)
        runCurrent()
        assertEquals(2, pollCalls())

        viewModel.closeNotes()
        advanceTimeBy(POLL_MS * 5)
        runCurrent()
        assertEquals(2, pollCalls())
        assertEquals(CapturePhase.ENDED, viewModel.state.value.phase)
    }

    @Test
    fun `server mode streams microphone frames instead of transcribing`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel, SttMode.SERVER)
        advanceUntilIdle()
        assertFalse(transcriber.running)
        assertEquals(listOf(0L, 1L), sockets.last.frames.map { it.seq })
        assertEquals(1600, sockets.last.frames.first().samples.size)
        // The fake microphone ran dry after two frames: reported as unavailable.
        assertEquals(MicProblem.UNAVAILABLE, viewModel.state.value.micProblem)
        viewModel.leave()
    }

    @Test
    fun `a degraded server recognizer is shown until it recovers or the socket says hello again`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel, SttMode.SERVER)
        val socket = sockets.last
        assertNull(viewModel.state.value.sttWarning)

        val lost = SttStatus(SttState.RECONNECTING, "Se ha perdido la conexión; se reintenta en 5 s.", 7)
        socket.receive(lost)
        runCurrent()
        assertEquals(lost, viewModel.state.value.sttWarning)

        socket.receive(SttStatus(SttState.OK, serverTimeMs = 8))
        runCurrent()
        assertNull(viewModel.state.value.sttWarning)

        socket.receive(SttStatus(SttState.UNAVAILABLE, serverTimeMs = 9))
        runCurrent()
        assertEquals(SttState.UNAVAILABLE, viewModel.state.value.sttWarning?.state)
        // A new handshake starts clean: the backend repeats a status that still holds after it.
        socket.receive(HelloAck("1.5", SttMode.SERVER, AudioFormat(), 0, clock.now))
        runCurrent()
        assertNull(viewModel.state.value.sttWarning)
        viewModel.leave()
    }

    @Test
    fun `a dropped socket reconnects while the transcriber keeps running`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel)
        sockets.last.drop()
        runCurrent()
        assertTrue(viewModel.state.value.connection is ConnectionState.Reconnecting)
        transcriber.emit(ClientTranscript.Final("and-x-0", 10, 30, "sin red"))
        advanceTimeBy(100)
        runCurrent()
        assertEquals(2, sockets.sockets.size)
        sockets.last.open()
        runCurrent()
        sockets.last.receive(HelloAck("1.1", SttMode.CLIENT, null, 0, clock.now))
        runCurrent()
        assertEquals(1, transcriber.starts)
        assertEquals(
            TranscriptClientFinal("and-x-0", 10, 30, "sin red", "android-speech", "es-ES"),
            sockets.last.sent.last(),
        )
        viewModel.leave()
    }

    @Test
    fun `leaving stops the socket and the microphone but keeps the session open`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel)
        viewModel.leave()
        runCurrent()
        assertTrue(sockets.last.closed)
        assertFalse(transcriber.running)
        assertEquals(CapturePhase.IDLE, viewModel.state.value.phase)
        assertEquals(open, holder.current.value)

        // "Continuar" on the same session starts again.
        connect(viewModel)
        assertEquals(2, sockets.sockets.size)
        assertTrue(transcriber.running)
        viewModel.leave()
    }

    @Test
    fun `a transcriber error is shown`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel)
        transcriber.fail(TranscriberError.PERMISSION_DENIED)
        assertEquals(MicProblem.PERMISSION_DENIED, viewModel.state.value.micProblem)
        viewModel.leave()
    }

    @Test
    fun `going to the background stops the transcriber and coming back restarts it`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel)
        assertEquals(1, transcriber.starts)

        viewModel.onBackground()
        runCurrent()
        assertFalse(transcriber.running)
        assertTrue(viewModel.state.value.micPaused)
        assertFalse(sockets.last.closed) // the session socket stays open

        viewModel.onForeground()
        runCurrent()
        assertTrue(transcriber.running)
        assertEquals(2, transcriber.starts)
        assertTrue(viewModel.state.value.micPaused) // «Micrófono en pausa» is shown on return...
        advanceTimeBy(CaptureViewModel.PAUSE_NOTICE_MS)
        runCurrent()
        assertFalse(viewModel.state.value.micPaused) // ...for a few seconds
        assertEquals(1, sockets.sockets.size)
        viewModel.leave()
    }

    @Test
    fun `the vocabulary hints of hello_ack reach the transcriber and a notice replaces them`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        viewModel.start()
        runCurrent()
        sockets.last.open()
        runCurrent()
        sockets.last.receive(HelloAck("1.5", SttMode.CLIENT, null, 0, clock.now, listOf("Historia", "El feudalismo")))
        runCurrent()
        assertEquals(listOf("Historia", "El feudalismo"), transcriber.hintsAtStart)
        assertEquals(listOf("Historia", "El feudalismo"), viewModel.vocabularyHints)

        // A notice without a list keeps the current one.
        sockets.last.receive(Notice(2, clock.now))
        runCurrent()
        assertEquals(listOf("Historia", "El feudalismo"), transcriber.vocabularyHints)

        val replaced = listOf("Historia", "El feudalismo", "vasallaje")
        sockets.last.receive(Notice(2, clock.now, replaced))
        runCurrent()
        assertEquals(replaced, transcriber.vocabularyHints)
        assertEquals(replaced, viewModel.vocabularyHints)

        // A restarted microphone starts with the latest list, not the one of hello.ack.
        viewModel.onBackground()
        viewModel.onForeground()
        runCurrent()
        assertEquals(2, transcriber.starts)
        assertEquals(replaced, transcriber.hintsAtStart)

        // A reconnect's hello.ack brings the backend's list again; one without hints keeps ours.
        sockets.last.drop()
        advanceTimeBy(100)
        runCurrent()
        sockets.last.open()
        runCurrent()
        sockets.last.receive(HelloAck("1.5", SttMode.CLIENT, null, 0, clock.now))
        runCurrent()
        assertEquals(replaced, transcriber.vocabularyHints)
        viewModel.leave()
    }

    @Test
    fun `ON_START without a preceding ON_STOP changes nothing`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        viewModel.onForeground() // the observer's first ON_START
        connect(viewModel)
        viewModel.onForeground()
        runCurrent()
        assertEquals(1, transcriber.starts)
        assertFalse(viewModel.state.value.micPaused)
        viewModel.leave()
    }

    @Test
    fun `an utterance in progress is sent once when pausing, and new ones continue after`() = runTest(main.dispatcher) {
        val engine = FakeRecognizerEngine()
        var created = 0
        val viewModel = CaptureViewModel(
            open = open,
            backendClient = backend,
            sessionHolder = holder,
            clock = clock,
            socketFactory = sockets,
            transcriberFactory = { scope -> SpeechRecognizerTranscriber(engine, clock, scope, segmentPrefix = "and-${created++}") },
            audioStreamerFactory = { AudioStreamer(audioSource, clock, main.dispatcher) },
            reconnectDelaysMs = listOf(100),
        )
        connect(viewModel)
        engine.listener.onSpeechStart()
        engine.listener.onPartial("el feudo")
        viewModel.onBackground()
        runCurrent()

        viewModel.onForeground()
        runCurrent()
        engine.listener.onSpeechStart()
        engine.listener.onResult("era la tierra", 0.9)
        runCurrent()

        val finals = sockets.last.sent.filterIsInstance<TranscriptClientFinal>()
        assertEquals(listOf("and-0-0" to "el feudo", "and-1-0" to "era la tierra"), finals.map { it.segmentId to it.text })
        viewModel.leave()
    }

    @Test
    fun `a reconnect while in the background does not restart the microphone`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel)
        viewModel.onBackground()
        sockets.last.drop()
        advanceTimeBy(100)
        runCurrent()
        sockets.last.open()
        runCurrent()
        sockets.last.receive(HelloAck("1.1", SttMode.CLIENT, null, 0, clock.now))
        runCurrent()
        assertFalse(transcriber.running)
        assertEquals(1, transcriber.starts)

        viewModel.onForeground()
        runCurrent()
        assertTrue(transcriber.running)
        viewModel.leave()
    }

    @Test
    fun `server mode stops streaming in the background and resumes the frame sequence after`() = runTest(main.dispatcher) {
        val viewModel = viewModel()
        connect(viewModel, SttMode.SERVER)
        runCurrent()
        val first = audioSource
        viewModel.onBackground()
        runCurrent()
        assertTrue(first.closed)

        audioSource = FakeAudioSource(totalSamples = 1600 * 2)
        viewModel.onForeground()
        runCurrent()
        assertTrue(audioSource.opened)
        assertEquals(listOf(0L, 1L, 2L, 3L), sockets.last.frames.map { it.seq })
        viewModel.leave()
    }

    private companion object {
        const val POLL_MS = 3_000L
    }
}
