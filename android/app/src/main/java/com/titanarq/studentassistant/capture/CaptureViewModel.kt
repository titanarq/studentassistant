package com.titanarq.studentassistant.capture

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.protocol.AudioFormat
import com.titanarq.studentassistant.protocol.Button
import com.titanarq.studentassistant.protocol.ButtonName
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.ClientAck
import com.titanarq.studentassistant.protocol.ClientCapabilities
import com.titanarq.studentassistant.protocol.Command
import com.titanarq.studentassistant.protocol.CommandName
import com.titanarq.studentassistant.protocol.Notice
import com.titanarq.studentassistant.protocol.ServerEvent
import com.titanarq.studentassistant.protocol.SessionEndReason
import com.titanarq.studentassistant.protocol.SessionEndRequest
import com.titanarq.studentassistant.protocol.SourceKind
import com.titanarq.studentassistant.protocol.SttMode
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import com.titanarq.studentassistant.protocol.TranscriptClientPartial
import com.titanarq.studentassistant.protocol.TranscriptFinal
import com.titanarq.studentassistant.protocol.TranscriptPartial
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.session.SessionHolder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** One line of the live transcript; a partial is shown greyed until its final replaces it. */
data class TranscriptLine(val segmentId: String, val text: String, val final: Boolean)

enum class CapturePhase {
    /** Waiting for [CaptureViewModel.start] (the microphone permission). */
    IDLE,

    RUNNING,

    /** "Terminar" pressed; the backend is ending the session. */
    ENDING,

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
) : ViewModel() {
    private val _state = MutableStateFlow(CaptureUiState(open.subjectName, open.topicName))
    val state: StateFlow<CaptureUiState> = _state.asStateFlow()

    private var connection: SessionConnection? = null
    private var jobs: List<Job> = emptyList()
    private var transcriber: ClientTranscriber? = null
    private var streamer: AudioStreamer? = null
    private var micMode: SttMode? = null
    private var inBackground = false
    private var pauseNoticeJob: Job? = null
    private val transcriptLines = LinkedHashMap<String, TranscriptLine>()

    /** Opens the session socket and, once `hello.ack` names the STT mode, the microphone. */
    fun start() {
        if (connection != null || _state.value.phase == CapturePhase.ENDED) return
        val connection = SessionConnection(
            scope = viewModelScope,
            socketFactory = socketFactory,
            url = SessionConnection.socketUrl(open.backend.baseUrl, open.session.wsPath),
            token = open.backend.token,
            clock = clock,
            capabilities = ClientCapabilities(
                stt = SttMode.CLIENT,
                sttProvider = SpeechRecognizerTranscriber.PROVIDER_ID,
                audioFormat = AudioFormat(),
            ),
            resume = {
                backendClient.resumeSession(open.backend, open.session.sessionId) is BackendResult.Success
            },
            reconnectDelaysMs = reconnectDelaysMs,
        )
        this.connection = connection
        _state.update { it.copy(phase = CapturePhase.RUNNING, micProblem = null) }
        jobs = listOf(
            viewModelScope.launch {
                connection.state.collect { state ->
                    _state.update { it.copy(connection = state) }
                    if (state is ConnectionState.Connected) startMic(state.sttMode)
                }
            },
            viewModelScope.launch { connection.events.collect(::onServerEvent) },
        )
        connection.start()
    }

    /** Leaves the screen: socket and microphone stop, the session stays open. */
    fun leave() {
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

    /** "Capturar": a burst of stills (#46). */
    fun capture() {
        if (_state.value.phase == CapturePhase.RUNNING) stillCapture.capture(CaptureTrigger.BUTTON, null)
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

    /** "Terminar": `button end_session`, then `POST /api/sessions/{id}/end`. */
    fun end() {
        if (_state.value.phase != CapturePhase.RUNNING) return
        _state.update { it.copy(phase = CapturePhase.ENDING, endFailure = null) }
        sendButton(ButtonName.END_SESSION, force = true)
        viewModelScope.launch {
            val result = backendClient.endSession(
                open.backend,
                open.session.sessionId,
                SessionEndRequest(clock.nowMillis(), SessionEndReason.BUTTON),
            )
            // 404/409: the session is already gone or ended (e.g. by voice); ended either way.
            val ended = result is BackendResult.Success ||
                (result is BackendResult.HttpError && result.status in ENDED_STATUSES)
            if (ended) {
                leave()
                sessionHolder.clear()
                _state.update { it.copy(phase = CapturePhase.ENDED) }
            } else {
                _state.update {
                    it.copy(phase = CapturePhase.RUNNING, endFailure = result as BackendResult.Failure)
                }
            }
        }
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
            is Notice -> _state.update { it.copy(pendingCount = event.pendingCount) }
            is Command -> when (event.command) {
                CommandName.CAPTURE_NOW -> {
                    stillCapture.capture(CaptureTrigger.COMMAND, event.commandId)
                    connection?.send(ClientAck(event.commandId, clock.nowMillis()))
                }
            }
            else -> Unit
        }
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

        private val ENDED_STATUSES = setOf(404, 409)

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
