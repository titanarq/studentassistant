package com.titanarq.studentassistant.capture

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.desk.DeskTopic
import com.titanarq.studentassistant.desk.DeskView
import com.titanarq.studentassistant.protocol.Button
import com.titanarq.studentassistant.protocol.ButtonName
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.Command
import com.titanarq.studentassistant.protocol.HelloAck
import com.titanarq.studentassistant.protocol.Notice
import com.titanarq.studentassistant.protocol.ServerAck
import com.titanarq.studentassistant.protocol.ServerEvent
import com.titanarq.studentassistant.protocol.SessionEndReason
import com.titanarq.studentassistant.protocol.SessionEndRequest
import com.titanarq.studentassistant.protocol.SourceKind
import com.titanarq.studentassistant.protocol.SttMode
import com.titanarq.studentassistant.protocol.SttState
import com.titanarq.studentassistant.protocol.SttStatus
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import com.titanarq.studentassistant.protocol.TranscriptClientPartial
import com.titanarq.studentassistant.protocol.TranscriptFinal
import com.titanarq.studentassistant.protocol.TranscriptPartial
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.session.SessionHolder
import com.titanarq.studentassistant.spool.AudioBacklog
import com.titanarq.studentassistant.spool.EventBacklog
import com.titanarq.studentassistant.spool.MemoryAudioBacklog
import com.titanarq.studentassistant.spool.MemoryEventBacklog
import com.titanarq.studentassistant.spool.PendingEnd
import kotlin.coroutines.CoroutineContext
import kotlin.coroutines.EmptyCoroutineContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/** One line of the live transcript; a partial is shown greyed until its final replaces it. */
data class TranscriptLine(val segmentId: String, val text: String, val final: Boolean)

enum class CapturePhase {
    /** Waiting for [CaptureViewModel.start] (the microphone permission). */
    IDLE,

    RUNNING,

    /** «Terminar captura» pressed; the backend is ending the session. */
    ENDING,

    /**
     * The session is over (the backend took the end, or the spool will deliver it): the screen
     * shows «Sesión terminada» with «Abrir en Construir» / «Volver al inicio» until the student
     * leaves it ([CaptureViewModel.closeEnded]).
     */
    ENDED,
}

/** Why the microphone is not feeding the session. */
enum class MicProblem {
    PERMISSION_DENIED,
    UNAVAILABLE,
}

data class CaptureUiState(
    val subjectName: String,
    val topicName: String,
    val phase: CapturePhase = CapturePhase.IDLE,
    val connection: ConnectionState = ConnectionState.Connecting,
    /** The latest lines, oldest first, at most [CaptureViewModel.MAX_TRANSCRIPT_LINES]. */
    val transcript: List<TranscriptLine> = emptyList(),
    /** Doubts awaiting review, from the last `notice`; null until one arrives. */
    val pendingCount: Int? = null,
    /** What the camera is looking at, toggled by "Libro/Apuntes". */
    val source: SourceKind = SourceKind.NOTES,
    val micProblem: MicProblem? = null,
    /** The last «Terminar captura» failed with this; the session is still open. */
    val endFailure: BackendResult.Failure? = null,
    /**
     * «Micrófono en pausa»: the app went to the background and the microphone stopped. Stays
     * true while in the background and for [CaptureViewModel.PAUSE_NOTICE_MS] after returning.
     */
    val micPaused: Boolean = false,
    /** The offline spool is close to its cap: the oldest audio is about to be dropped. */
    val spoolNearCap: Boolean = false,
    /**
     * Since protocol 1.5 (#222), server STT mode: the backend's own recognizer is not transcribing
     * (its last degraded `stt.status`); null while it works. A new `hello.ack` clears it, since the
     * backend repeats a status that still holds right after it.
     */
    val sttWarning: SttStatus? = null,
)

/**
 * Where the capture screen keeps what the backend may not have yet (android-offline, #53): the
 * session's [audio] and [events] backlogs on disk, the shared cap's [nearCap] flag, the
 * [finisher] that completes an end the backend could not take, and the [loopContext] (an I/O
 * dispatcher) the connection runs on.
 */
class CaptureSpooling(
    val audio: AudioBacklog,
    val events: EventBacklog,
    val nearCap: StateFlow<Boolean>,
    val finisher: SessionFinisher,
    val loopContext: CoroutineContext = EmptyCoroutineContext,
)

/**
 * The capture screen (ADR-0001, ADR-0008): the session WebSocket, the microphone in the STT mode
 * the backend chose (client: [ClientTranscriber] segments; server: [AudioStreamer] frames), the
 * live transcript and pending counter from server events, and the session buttons.
 *
 * [start] runs once the microphone permission is granted; [leave] stops everything without ending
 * the session (the home screen offers "Continuar"); [end] ends it. [onBackground] / [onForeground]
 * (the screen's `ON_STOP` / `ON_START`) pause and resume the microphone while the socket stays
 * open, so the session goes on where it was.
 *
 * With [spooling] (the app), audio, finals and queued events go through its disk backlogs, so they
 * survive the process dying and are resent when the session is continued. «Terminar captura» while
 * offline, or answered with a transient failure, becomes a [PendingEnd] that the
 * [SessionFinisher] completes when the backend is back: the screen ends at once. Online,
 * «Terminar captura» first waits (at most [END_FLUSH_TIMEOUT_MS]) for the connection to drain and the
 * session's captures to upload. Without it (tests), everything stays in memory.
 *
 * Ending only ends the capture (#431): nothing on the phone prepares the notes. The end request
 * never carries `prepare_notes`; after it the screen stays in [CapturePhase.ENDED] and offers the
 * topic's «Construir» screen ([deskTopic]), where the student asks for the notes through the chat.
 * The open session is released ([SessionHolder.clear]) when the student leaves that screen
 * ([closeEnded], [leave]).
 */
class CaptureViewModel(
    private val open: OpenSession,
    private val backendClient: BackendClient,
    private val sessionHolder: SessionHolder,
    private val clock: Clock,
    private val socketFactory: SessionSocketFactory,
    private val transcriberFactory: (CoroutineScope) -> ClientTranscriber,
    private val audioStreamerFactory: () -> AudioStreamer,
    private val stillCapture: StillCapture = NoStillCapture,
    private val reconnectDelaysMs: List<Long> = SessionConnection.DEFAULT_RECONNECT_DELAYS_MS,
    private val spooling: CaptureSpooling? = null,
) : ViewModel() {
    private val _state = MutableStateFlow(CaptureUiState(open.subjectName, open.topicName))
    val state: StateFlow<CaptureUiState> = _state.asStateFlow()

    init {
        spooling?.let { spooling ->
            viewModelScope.launch { spooling.nearCap.collect { near -> _state.update { it.copy(spoolNearCap = near) } } }
        }
    }

    private var resumedOnce = false

    /** This session's captures for the thumbnail strip, oldest first, with their upload state. */
    val shots: StateFlow<List<CaptureShot>> =
        stillCapture.shots.stateIn(viewModelScope, SharingStarted.WhileSubscribed(SHOTS_STOP_TIMEOUT_MS), emptyList())

    private var connection: SessionConnection? = null
    private var jobs: List<Job> = emptyList()
    private var transcriber: ClientTranscriber? = null
    private var streamer: AudioStreamer? = null
    private var micMode: SttMode? = null
    private var inBackground = false
    private var pauseNoticeJob: Job? = null
    private val transcriptLines = LinkedHashMap<String, TranscriptLine>()

    /**
     * The session's latest vocabulary hints (protocol 1.4, #228): those of the last `hello.ack`,
     * replaced by every `notice` that carries a list; a missing list keeps the current one.
     */
    var vocabularyHints: List<String> = emptyList()
        private set

    /** Opens the session socket and, once `hello.ack` names the STT mode, the microphone. */
    fun start() {
        if (connection != null || _state.value.phase == CapturePhase.ENDED) return
        val connection = SessionConnection(
            scope = viewModelScope,
            socketFactory = socketFactory,
            url = SessionConnection.socketUrl(open.backend.baseUrl, open.session.wsPath),
            token = open.backend.token,
            userId = open.backend.userId,
            clock = clock,
            capabilities = SessionConnection.CAPTURE_CAPABILITIES,
            resume = {
                val resumed = backendClient.resumeSession(open.backend, open.session.sessionId)
                if (resumed is BackendResult.Success) stillCapture.resumed(resumed.value.receivedCaptureIds)
                resumed is BackendResult.Success
            },
            reconnectDelaysMs = reconnectDelaysMs,
            audio = spooling?.audio ?: MemoryAudioBacklog(SessionConnection.MAX_BUFFERED_FRAMES),
            backlog = spooling?.events ?: MemoryEventBacklog(),
            loopContext = spooling?.loopContext ?: EmptyCoroutineContext,
        )
        this.connection = connection
        if (!resumedOnce) {
            resumedOnce = true
            stillCapture.resumed(open.session.receivedCaptureIds)
        }
        _state.update { it.copy(phase = CapturePhase.RUNNING, micProblem = null) }
        jobs = listOf(
            viewModelScope.launch {
                connection.state.collect { state ->
                    _state.update { it.copy(connection = state) }
                    if (state is ConnectionState.Connected) {
                        // Only a new `hello.ack` brings hints here: a later `notice` may have replaced them.
                        state.vocabularyHints?.let(::useVocabularyHints)
                        startMic(state.sttMode)
                        // A new connection counts as sending (#425): in the background, say so again.
                        if (inBackground) connection.send(Button(ButtonName.PAUSE, null, clock.nowMillis()))
                    }
                }
            },
            viewModelScope.launch { connection.events.collect(::onServerEvent) },
        )
        connection.start()
    }

    /**
     * What «Abrir en Construir» opens in the study desk after the end: the topic's «Construir»
     * screen (its workspace, #414), where the student asks for the notes through the chat.
     */
    val deskTopic: DeskTopic
        get() = DeskTopic(open.session.subjectId, open.session.topicId, open.topicName, DeskView.WORKSPACE)

    /**
     * Leaves the screen: socket and microphone stop, the session stays open. In
     * [CapturePhase.ENDED] it is [closeEnded].
     */
    fun leave() {
        if (_state.value.phase == CapturePhase.ENDED) {
            closeEnded()
            return
        }
        stopMic()
        jobs.forEach { it.cancel() }
        jobs = emptyList()
        connection?.stop()
        connection = null
        if (_state.value.phase == CapturePhase.RUNNING) _state.update { it.copy(phase = CapturePhase.IDLE) }
    }

    /**
     * The app went to the background (`ON_STOP`): the microphone stops (an utterance in progress
     * is settled as a final and sent first). The socket stays open and keeps its buffers, so
     * nothing is lost or sent twice. Since #425 it also says `button: pause`, so the backend knows
     * this client stopped sending (and ends the session on its own if nothing sends for long).
     */
    fun onBackground() {
        if (inBackground) return
        inBackground = true
        pauseNoticeJob?.cancel()
        pauseNoticeJob = null
        val wasListening = micMode != null
        stopMic()
        if (wasListening || _state.value.phase == CapturePhase.RUNNING) _state.update { it.copy(micPaused = true) }
        sendButton(ButtonName.PAUSE)
    }

    /**
     * Back in the foreground (`ON_START`): the microphone restarts in the connection's STT mode
     * (or at the next `hello.ack` when not connected now); «Micrófono en pausa» stays for
     * [PAUSE_NOTICE_MS]. Since #425 it says `button: resume` first.
     */
    fun onForeground() {
        if (!inBackground) return
        inBackground = false
        sendButton(ButtonName.RESUME)
        val state = connection?.state?.value
        if (state is ConnectionState.Connected) startMic(state.sttMode)
        if (_state.value.micPaused) {
            pauseNoticeJob = viewModelScope.launch {
                delay(PAUSE_NOTICE_MS)
                _state.update { it.copy(micPaused = false) }
            }
        }
    }

    /** From a failed connection: try again now. */
    fun retry() {
        connection?.retry()
    }

    /** "Capturar": a burst of stills, uploaded in the background. */
    fun capture() {
        if (_state.value.phase == CapturePhase.RUNNING) stillCapture.capture(CaptureTrigger.BUTTON, null)
    }

    /** A tap on a failed thumbnail: upload it again (same `capture_id`). */
    fun retryShot(captureId: String) {
        stillCapture.retry(captureId)
    }

    /** "Importante": flags this moment. */
    fun important() {
        sendButton(ButtonName.IMPORTANT)
    }

    /** "Libro/Apuntes": switches between the textbook and the notebook. */
    fun toggleSource() {
        if (_state.value.phase != CapturePhase.RUNNING) return
        val next = if (_state.value.source == SourceKind.NOTES) SourceKind.BOOK else SourceKind.NOTES
        _state.update { it.copy(source = next) }
        sendButton(ButtonName.SWITCH_SOURCE, next)
    }

    /**
     * «Terminar captura»: `button end_session`, then `POST /api/sessions/{id}/end` without
     * `prepare_notes`. With
     * [spooling], an end the backend cannot take now is handed to the [SessionFinisher] (see the
     * class doc). Either way the screen then shows «Sesión terminada» ([CapturePhase.ENDED]).
     */
    fun end() {
        if (_state.value.phase != CapturePhase.RUNNING) return
        _state.update { it.copy(phase = CapturePhase.ENDING, endFailure = null) }
        val endedAtMs = clock.nowMillis()
        sendButton(ButtonName.END_SESSION, force = true)
        val connection = connection
        val spooling = spooling
        if (spooling != null && connection?.state?.value !is ConnectionState.Connected) {
            handOffEnd(spooling, endedAtMs)
            return
        }
        viewModelScope.launch {
            if (spooling != null && connection != null) {
                stopMic()
                withTimeoutOrNull(END_FLUSH_TIMEOUT_MS) {
                    connection.drained.first { it }
                    stillCapture.awaitUploads()
                }
            }
            val result = backendClient.endSession(
                open.backend,
                open.session.sessionId,
                SessionEndRequest(endedAtMs, SessionEndReason.BUTTON),
            )
            // 404/409: the session is already gone or ended (e.g. by voice); ended either way.
            val ended = result is BackendResult.Success ||
                (result is BackendResult.HttpError && result.status in ENDED_STATUSES)
            when {
                ended -> {
                    leave()
                    spooling?.finisher?.ended(open.session.sessionId)
                    _state.update { it.copy(phase = CapturePhase.ENDED) }
                }
                spooling != null && CaptureUploadQueue.isTransient(result as BackendResult.Failure) &&
                    !(result is BackendResult.HttpError && result.status == 409) -> handOffEnd(spooling, endedAtMs)
                else -> {
                    val state = connection?.state?.value
                    if (state is ConnectionState.Connected) startMic(state.sttMode)
                    _state.update {
                        it.copy(phase = CapturePhase.RUNNING, endFailure = result as BackendResult.Failure)
                    }
                }
            }
        }
    }

    /**
     * Leaves «Sesión terminada» («Abrir en Construir», «Volver al inicio», back): the open session
     * is released, so the home screen offers no «Continuar» for it.
     */
    fun closeEnded() {
        if (_state.value.phase != CapturePhase.ENDED) return
        sessionHolder.clear()
    }

    /** The backend cannot take the end now: the finisher completes it once the spool is flushed. */
    private fun handOffEnd(spooling: CaptureSpooling, endedAtMs: Long) {
        leave()
        spooling.finisher.finish(
            open.backend,
            PendingEnd(open.session.sessionId, open.backend.baseUrl, endedAtMs, SessionEndReason.BUTTON, userId = open.backend.userId),
        )
        _state.update { it.copy(phase = CapturePhase.ENDED) }
    }

    private fun sendButton(button: ButtonName, source: SourceKind? = null, force: Boolean = false) {
        if (!force && _state.value.phase != CapturePhase.RUNNING) return
        connection?.send(Button(button, source, clock.nowMillis()))
    }

    private fun startMic(mode: SttMode) {
        if (micMode == mode || inBackground) return
        stopMic()
        micMode = mode
        val connection = connection ?: return
        when (mode) {
            SttMode.CLIENT -> {
                val transcriber = transcriberFactory(viewModelScope).also { transcriber = it }
                transcriber.vocabularyHints = vocabularyHints
                transcriber.start(
                    onTranscript = { transcript -> connection.send(toEvent(transcript, transcriber)) },
                    onError = { error ->
                        val problem = when (error) {
                            TranscriberError.PERMISSION_DENIED -> MicProblem.PERMISSION_DENIED
                            TranscriberError.UNAVAILABLE -> MicProblem.UNAVAILABLE
                        }
                        _state.update { it.copy(micProblem = problem) }
                    },
                )
            }
            SttMode.SERVER -> {
                val streamer = audioStreamerFactory().also { streamer = it }
                streamer.start(
                    viewModelScope,
                    onFrame = connection::sendAudio,
                    onError = { _state.update { it.copy(micProblem = MicProblem.UNAVAILABLE) } },
                )
            }
        }
    }

    private fun stopMic() {
        transcriber?.stop()
        transcriber = null
        streamer?.stop()
        streamer = null
        micMode = null
    }

    private fun onServerEvent(event: ServerEvent) {
        when (event) {
            is TranscriptPartial -> showLine(TranscriptLine(event.segmentId, event.text, final = false))
            is TranscriptFinal -> showLine(TranscriptLine(event.segmentId, event.text, final = true))
            is Notice -> {
                event.vocabularyHints?.let(::useVocabularyHints)
                _state.update { it.copy(pendingCount = event.pendingCount) }
            }
            is HelloAck -> _state.update { it.copy(sttWarning = null) }
            is SttStatus -> _state.update { it.copy(sttWarning = event.takeIf { e -> e.state != SttState.OK }) }
            is ServerAck -> event.captureIds?.let(stillCapture::confirmReceived)
            // #556: a capture is taken only by «Capturar». The backend still publishes
            // `capture_now` (the voice command, the web capture page obeys it), so the command
            // arrives here: it is deliberately neither acted on nor acknowledged.
            is Command -> Unit
            else -> Unit
        }
    }

    /** Keeps [hints] and biases the running recognizer towards them from its next round. */
    private fun useVocabularyHints(hints: List<String>) {
        vocabularyHints = hints
        transcriber?.vocabularyHints = hints
    }

    private fun showLine(line: TranscriptLine) {
        // A final never goes back to a partial (a late partial of a settled segment).
        if (transcriptLines[line.segmentId]?.final == true && !line.final) return
        transcriptLines[line.segmentId] = line
        while (transcriptLines.size > MAX_TRANSCRIPT_LINES) transcriptLines.remove(transcriptLines.keys.first())
        _state.update { it.copy(transcript = transcriptLines.values.toList()) }
    }

    override fun onCleared() {
        leave()
    }

    companion object {
        const val MAX_TRANSCRIPT_LINES: Int = 50

        /** How long «Micrófono en pausa» stays after returning to the foreground. */
        const val PAUSE_NOTICE_MS: Long = 4_000

        private const val SHOTS_STOP_TIMEOUT_MS = 5_000L

        private val ENDED_STATUSES = setOf(404, 409)

        /** How long an online «Terminar captura» waits for the spool to drain and captures to upload. */
        const val END_FLUSH_TIMEOUT_MS: Long = 10_000

        internal fun toEvent(transcript: ClientTranscript, transcriber: ClientTranscriber) = when (transcript) {
            is ClientTranscript.Partial -> TranscriptClientPartial(
                transcript.segmentId,
                transcript.clientStartMs,
                transcript.clientEndMs,
                transcript.text,
                transcriber.providerId,
                transcriber.language,
                transcript.confidence,
            )
            is ClientTranscript.Final -> TranscriptClientFinal(
                transcript.segmentId,
                transcript.clientStartMs,
                transcript.clientEndMs,
                transcript.text,
                transcriber.providerId,
                transcriber.language,
                transcript.confidence,
            )
        }
    }
}
