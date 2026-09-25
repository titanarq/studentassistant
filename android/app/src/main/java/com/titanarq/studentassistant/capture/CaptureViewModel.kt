package com.titanarq.studentassistant.capture

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.desk.DeskTopic
import com.titanarq.studentassistant.protocol.Button
import com.titanarq.studentassistant.protocol.ButtonName
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.ClientAck
import com.titanarq.studentassistant.protocol.Command
import com.titanarq.studentassistant.protocol.CommandName
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

    /** "Terminar" pressed; the backend is ending the session. */
    ENDING,

    /**
     * "Terminar y preparar apuntes" was delivered: the session is over and the screen shows the
     * notes generation's progress ([CaptureUiState.notesProgress]) until the student leaves it.
     */
    NOTES,

    /** The session is over; the screen goes back home. */
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
    /** The last "Terminar" failed with this; the session is still open. */
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
    /** In [CapturePhase.NOTES], the notes generation the end started. */
    val notesProgress: NotesProgress? = null,
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
 * survive the process dying and are resent when the session is continued. "Terminar" while
 * offline, or answered with a transient failure, becomes a [PendingEnd] that the
 * [SessionFinisher] completes when the backend is back: the screen ends at once. Online,
 * "Terminar" first waits (at most [END_FLUSH_TIMEOUT_MS]) for the connection to drain and the
 * session's captures to upload. Without it (tests), everything stays in memory.
 *
 * "Terminar y preparar apuntes" (`end(prepareNotes = true)`, #272) sends the end with
 * `prepare_notes` (a pending end keeps the flag). When the backend takes it at once the screen
 * stays in [CapturePhase.NOTES] and polls the topic's notes generation every
 * [notesPollIntervalMs] (paused in the background) until it finishes or the student leaves
 * ([closeNotes], [leave]).
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
    private val notesPollIntervalMs: Long = NotesGenerationPoller.DEFAULT_INTERVAL_MS,
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
    private var notesPollJob: Job? = null
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
                    }
                }
            },
            viewModelScope.launch { connection.events.collect(::onServerEvent) },
        )
        connection.start()
    }

    /** The topic whose notes "Abrir apuntes" opens in the study desk. */
    val deskTopic: DeskTopic get() = DeskTopic(open.session.subjectId, open.session.topicId, open.topicName)

    /**
     * Leaves the screen: socket and microphone stop, the session stays open. In
     * [CapturePhase.NOTES] it is [closeNotes].
     */
    fun leave() {
        if (_state.value.phase == CapturePhase.NOTES) {
            closeNotes()
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
     * nothing is lost or sent twice.
     */
    fun onBackground() {
        if (inBackground) return
        inBackground = true
        pauseNoticeJob?.cancel()
        pauseNoticeJob = null
        val wasListening = micMode != null
        stopMic()
        stopNotesPolling()
        if (wasListening || _state.value.phase == CapturePhase.RUNNING) _state.update { it.copy(micPaused = true) }
    }

    /**
     * Back in the foreground (`ON_START`): the microphone restarts in the connection's STT mode
     * (or at the next `hello.ack` when not connected now); «Micrófono en pausa» stays for
     * [PAUSE_NOTICE_MS].
     */
    fun onForeground() {
        if (!inBackground) return
        inBackground = false
        val state = connection?.state?.value
        if (state is ConnectionState.Connected) startMic(state.sttMode)
        pollNotes()
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
     * "Terminar": `button end_session`, then `POST /api/sessions/{id}/end`, with `prepare_notes`
     * when [prepareNotes] ("Terminar y preparar apuntes"). With [spooling], an end the backend
     * cannot take now is handed to the [SessionFinisher] (see the class doc).
     */
    fun end(prepareNotes: Boolean = false) {
        if (_state.value.phase != CapturePhase.RUNNING) return
        _state.update { it.copy(phase = CapturePhase.ENDING, endFailure = null) }
        val endedAtMs = clock.nowMillis()
        sendButton(ButtonName.END_SESSION, force = true)
        val connection = connection
        val spooling = spooling
        if (spooling != null && connection?.state?.value !is ConnectionState.Connected) {
            handOffEnd(spooling, endedAtMs, prepareNotes)
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
                SessionEndRequest(endedAtMs, SessionEndReason.BUTTON, prepareNotes = true.takeIf { prepareNotes }),
            )
            // 404/409: the session is already gone or ended (e.g. by voice); ended either way.
            val ended = result is BackendResult.Success ||
                (result is BackendResult.HttpError && result.status in ENDED_STATUSES)
            when {
                prepareNotes && result is BackendResult.Success -> {
                    leave()
                    spooling?.finisher?.ended(open.session.sessionId)
                    // The holder is cleared when the student leaves the progress (closeNotes).
                    _state.update {
                        it.copy(phase = CapturePhase.NOTES, notesProgress = NotesProgress.fromStart(result.value.notesGeneration))
                    }
                    pollNotes()
                }
                ended -> {
                    // A 404/409 started no generation: nothing to follow.
                    leave()
                    spooling?.finisher?.ended(open.session.sessionId)
                    sessionHolder.clear()
                    _state.update { it.copy(phase = CapturePhase.ENDED) }
                }
                spooling != null && CaptureUploadQueue.isTransient(result as BackendResult.Failure) &&
                    !(result is BackendResult.HttpError && result.status == 409) -> handOffEnd(spooling, endedAtMs, prepareNotes)
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
     * Leaves the notes progress ("Volver", "Abrir apuntes", back): polling stops and the screen
     * ends. The generation goes on in the backend.
     */
    fun closeNotes() {
        if (_state.value.phase != CapturePhase.NOTES) return
        stopNotesPolling()
        sessionHolder.clear()
        _state.update { it.copy(phase = CapturePhase.ENDED) }
    }

    /** In [CapturePhase.NOTES], in the foreground, while the generation runs: polls it. */
    private fun pollNotes() {
        val state = _state.value
        if (state.phase != CapturePhase.NOTES || inBackground || notesPollJob?.isActive == true) return
        if (state.notesProgress?.finished != false) return
        val poller = NotesGenerationPoller(
            backendClient,
            open.backend,
            open.session.subjectId,
            open.session.topicId,
            notesPollIntervalMs,
        )
        notesPollJob = viewModelScope.launch {
            poller.poll { progress -> _state.update { it.copy(notesProgress = progress) } }
        }
    }

    private fun stopNotesPolling() {
        notesPollJob?.cancel()
        notesPollJob = null
    }

    /** The backend cannot take the end now: the finisher completes it once the spool is flushed. */
    private fun handOffEnd(spooling: CaptureSpooling, endedAtMs: Long, prepareNotes: Boolean) {
        leave()
        spooling.finisher.finish(
            open.backend,
            PendingEnd(open.session.sessionId, open.backend.baseUrl, endedAtMs, SessionEndReason.BUTTON, prepareNotes),
        )
        sessionHolder.clear()
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
            is Command -> when (event.command) {
                CommandName.CAPTURE_NOW -> {
                    stillCapture.capture(CaptureTrigger.COMMAND, event.commandId)
                    connection?.send(ClientAck(event.commandId, clock.nowMillis()))
                }
            }
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

        /** How long an online "Terminar" waits for the spool to drain and captures to upload. */
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
