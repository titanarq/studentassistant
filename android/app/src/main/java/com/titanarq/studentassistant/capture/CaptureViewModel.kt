package com.titanarq.studentassistant.capture

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendResult
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
import kotlinx.coroutines.flow.combine
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

    /** The student left the screen; the backend is ending the session. */
    ENDING,

    /** The session is over (the backend took the end, or the spool will deliver it). */
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
 * live transcript and pending counter from server events (kept running, but the screen shows
 * neither), and the capture trigger.
 *
 * The screen has no session buttons: entering it starts the capture ([start]) and leaving it ends
 * the session ([endOnLeave]). [leave] stops everything without ending the session. [onBackground] / [onForeground]
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
 * never carries `prepare_notes`. The open session is released ([SessionHolder.clear]) once the end
 * is taken by the backend or handed to the spool.
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
    private val thumbnails: ThumbnailStore? = null,
) : ViewModel() {
    private val _state = MutableStateFlow(CaptureUiState(open.subjectName, open.topicName))
    val state: StateFlow<CaptureUiState> = _state.asStateFlow()

    init {
        thumbnails?.let { store ->
            val key = ThumbnailKey(open.session.subjectId, open.session.topicId)
            viewModelScope.launch {
                stillCapture.shots.collect { list ->
                    list.forEach { shot -> StoredThumbnail.of(shot)?.let { store.add(key, it) } }
                }
            }
        }
        spooling?.let { spooling ->
            viewModelScope.launch { spooling.nearCap.collect { near -> _state.update { it.copy(spoolNearCap = near) } } }
        }
    }

    private var resumedOnce = false

    /** Set once the student left the screen: the session is released as soon as it has ended. */
    private var releaseWhenEnded = false

    /** This session's captures for the thumbnail strip, oldest first, with their upload state. */
    val shots: StateFlow<List<CaptureShot>> = run {
        val key = ThumbnailKey(open.session.subjectId, open.session.topicId)
        val merged = if (thumbnails == null) {
            stillCapture.shots
        } else {
            // Photos of earlier visits to this topic first (#583), then this session's own.
            combine(stillCapture.shots, thumbnails.flow(key)) { current, earlier ->
                val own = current.mapTo(HashSet()) { it.captureId }
                earlier.filter { it.captureId !in own }.map(StoredThumbnail::toShot) + current
            }
        }
        merged.stateIn(viewModelScope, SharingStarted.WhileSubscribed(SHOTS_STOP_TIMEOUT_MS), emptyList())
    }

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

    /**
     * The student left the capture screen: the session ends. `button end_session`, then
     * `POST /api/sessions/{id}/end` without `prepare_notes`. With [spooling], an end the backend
     * cannot take now is handed to the [SessionFinisher] (see the class doc). The session is
     * released from [SessionHolder] when the end is done, so the screen can already be gone; an
     * end refused for a non-transient reason stops the socket and releases the session anyway (the
     * backend closes it on its own after a while).
     */
    fun endOnLeave() {
        releaseWhenEnded = true
        when (_state.value.phase) {
            CapturePhase.ENDED -> closeEnded()
            CapturePhase.ENDING -> Unit
            CapturePhase.RUNNING -> {
                val spooling = spooling
                if (spooling != null) {
                    // The end must outlive this screen (#583): its view model scope dies with the screen, so
                    // the app-wide finisher takes it (written to disk first, retried until the backend
                    // answers) instead of an HTTP call that the screen's cancellation could drop.
                    _state.update { it.copy(phase = CapturePhase.ENDING, endFailure = null) }
                    val endedAtMs = clock.nowMillis()
                    sendButton(ButtonName.END_SESSION, force = true)
                    handOffEnd(spooling, endedAtMs)
                } else {
                    end()
                }
            }
            CapturePhase.IDLE -> {
                // The screen never started the session (no camera permission): end it all the same.
                _state.update { it.copy(phase = CapturePhase.ENDING) }
                val endedAtMs = clock.nowMillis()
                val spooling = spooling
                if (spooling != null) {
                    handOffEnd(spooling, endedAtMs)
                } else {
                    viewModelScope.launch {
                        backendClient.endSession(
                            open.backend,
                            open.session.sessionId,
                            SessionEndRequest(endedAtMs, SessionEndReason.BUTTON),
                        )
                        markEnded()
                    }
                }
            }
        }
    }

    /** Ends the session now ([endOnLeave] is the screen's entry point). */
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
                    markEnded()
                }
                spooling != null && CaptureUploadQueue.isTransient(result as BackendResult.Failure) &&
                    !(result is BackendResult.HttpError && result.status == 409) -> handOffEnd(spooling, endedAtMs)
                releaseWhenEnded -> {
                    leave()
                    markEnded()
                }
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
     * Safety net (#583): a view model dropped while its session still runs (the screen went away
     * by any route) must not leave the session open on the backend.
     */
    override fun onCleared() {
        if (_state.value.phase == CapturePhase.RUNNING) {
            val spooling = spooling
            if (spooling != null) {
                releaseWhenEnded = true
                _state.update { it.copy(phase = CapturePhase.ENDING) }
                handOffEnd(spooling, clock.nowMillis())
            }
        }
        leave()
        super.onCleared()
    }

    private fun markEnded() {
        _state.update { it.copy(phase = CapturePhase.ENDED) }
        if (releaseWhenEnded) sessionHolder.clear()
    }

    /** The open session is released, so nothing offers it any more. */
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
        markEnded()
    }

    private fun sendButton(button: ButtonName, force: Boolean = false) {
        if (!force && _state.value.phase != CapturePhase.RUNNING) return
        connection?.send(Button(button, null, clock.nowMillis()))
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
