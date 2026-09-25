package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.protocol.AudioFormat
import com.titanarq.studentassistant.protocol.AudioFrame
import com.titanarq.studentassistant.protocol.ClientCapabilities
import com.titanarq.studentassistant.protocol.ClientEvent
import com.titanarq.studentassistant.protocol.Hello
import com.titanarq.studentassistant.protocol.HelloAck
import com.titanarq.studentassistant.protocol.PROTOCOL_VERSION
import com.titanarq.studentassistant.protocol.ServerAck
import com.titanarq.studentassistant.protocol.ServerEvent
import com.titanarq.studentassistant.protocol.SttMode
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import com.titanarq.studentassistant.protocol.TranscriptClientPartial
import com.titanarq.studentassistant.protocol.TranscriptFinal
import com.titanarq.studentassistant.protocol.decodeServerEvent
import com.titanarq.studentassistant.protocol.encodeClientEvent
import com.titanarq.studentassistant.spool.AudioBacklog
import com.titanarq.studentassistant.spool.EventBacklog
import com.titanarq.studentassistant.spool.MemoryAudioBacklog
import com.titanarq.studentassistant.spool.MemoryEventBacklog
import com.titanarq.studentassistant.spool.SpooledFrame
import kotlin.coroutines.CoroutineContext
import kotlin.coroutines.EmptyCoroutineContext
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** Where the session WebSocket stands. */
sealed interface ConnectionState {
    /** Opening the first socket, or waiting for its `hello.ack`. */
    data object Connecting : ConnectionState

    /** `hello.ack` received: the backend chose [sttMode]. */
    data class Connected(val sttMode: SttMode, val clockOffsetMs: Long) : ConnectionState

    /** The socket dropped; attempt [attempt] opens after a back-off. */
    data class Reconnecting(val attempt: Int, val reason: String) : ConnectionState

    /** Gave up; [retry][SessionConnection.retry] tries again. */
    data class Failed(val failure: ConnectionFailure) : ConnectionState

    /** [stop][SessionConnection.stop] was called. */
    data object Stopped : ConnectionState
}

sealed interface ConnectionFailure {
    /** The WebSocket handshake was refused (bad or revoked token). */
    data class Unauthorized(val reason: String) : ConnectionFailure

    /** The session is no longer open on the backend and could not be resumed. */
    data object SessionNotActive : ConnectionFailure

    /** The backend closed the socket as a contract violation (e.g. incompatible version). */
    data class Refused(val reason: String) : ConnectionFailure
}

/**
 * The client side of `/ws/sessions/{id}` (protocol v1): `hello`, `hello.ack`, every client event,
 * the server events on [events], audio frames in server STT mode, and reconnection.
 *
 * What the backend may not have yet lives in two backlogs, in memory by default or on disk (the
 * [com.titanarq.studentassistant.spool] classes) so it survives the app process dying:
 * - [backlog]: transcript finals until the backend echoes the `transcript.final` with the same
 *   `segment_id`; they are resent after every `hello.ack` in client mode (the backend drops a final
 *   it saw already, without echoing it, so a resent final not echoed within [finalGraceMs] --
 *   more than two ping intervals -- of a connection that stayed up counts as held). `button`, `marker` and `ack` sent while
 *   disconnected are queued and sent after `hello.ack`. Partials are dropped while disconnected
 *   (their final supersedes them).
 * - [audio]: every frame until the server `ack`'s `audio_seq` covers it. After a `hello.ack` in
 *   server mode with frames held, the connection waits up to [ackWaitMs] for the `ack` the backend
 *   sends right after it whenever it holds audio (where it stands), then resends in `seq` order
 *   everything after the acknowledged `seq`, a batch at a time while the socket's send queue is under
 *   [maxSocketQueueBytes], and only then live frames (a live frame produced meanwhile joins the
 *   backlog behind them). Renumbering happens only on a real `ack`: when the held frames do not
 *   follow its `seq` and none past it was sent on this socket (older audio was dropped at the
 *   spool's cap), or when it covers more frames than this connection produced (a restarted app on
 *   a resumed session without a spool), the held frames are renumbered right after it; the backend
 *   places audio by client time, never by `seq`, and never skips a missing `seq`. Without an
 *   `ack` in time the frames go out as numbered (renumbered from 0 only when this backlog never
 *   saw any `ack` and its first frame is not 0).
 *
 * [drained] is true while connected with nothing left to deliver in the negotiated mode.
 *
 * A close with code 4404 (the session is not active, e.g. the backend restarted) runs [resume]
 * (the REST resume) and reconnects when it returns true.
 *
 * Everything runs on one coroutine of [scope] (plus [loopContext], e.g. an I/O dispatcher for a
 * disk backlog) fed by a channel, so socket callbacks (any thread) and callers (any thread) never
 * race. [start] opens the first socket; [stop] closes it and keeps the backlogs as they are.
 */
class SessionConnection(
    private val scope: CoroutineScope,
    private val socketFactory: SessionSocketFactory,
    private val url: String,
    private val token: String,
    private val clock: Clock,
    private val capabilities: ClientCapabilities,
    private val resume: suspend () -> Boolean = { false },
    private val reconnectDelaysMs: List<Long> = DEFAULT_RECONNECT_DELAYS_MS,
    private val audio: AudioBacklog = MemoryAudioBacklog(MAX_BUFFERED_FRAMES),
    private val backlog: EventBacklog = MemoryEventBacklog(MAX_UNCONFIRMED_FINALS, MAX_QUEUED_EVENTS),
    private val loopContext: CoroutineContext = EmptyCoroutineContext,
    private val ackWaitMs: Long = ACK_WAIT_MS,
    private val finalGraceMs: Long = FINAL_GRACE_MS,
    private val maxSocketQueueBytes: Long = MAX_SOCKET_QUEUE_BYTES,
    private val protocolVersion: String = PROTOCOL_VERSION,
) {
    private val _state = MutableStateFlow<ConnectionState>(ConnectionState.Connecting)
    val state: StateFlow<ConnectionState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<ServerEvent>(extraBufferCapacity = 256)

    /** Every server event after `hello.ack` (and `hello.ack` itself), in arrival order. */
    val events: SharedFlow<ServerEvent> = _events.asSharedFlow()

    private val _drained = MutableStateFlow(false)

    /**
     * True while connected with nothing left to deliver: no queued event and, in client mode, no
     * unconfirmed final, in server mode, no unacknowledged frame.
     */
    val drained: StateFlow<Boolean> = _drained.asStateFlow()

    private sealed interface Input {
        data object Start : Input
        data object Retry : Input
        data object Stop : Input
        data class Opened(val generation: Int) : Input
        data class Text(val generation: Int, val text: String) : Input
        data class Closed(val generation: Int, val code: Int?, val reason: String) : Input
        data class Reconnect(val generation: Int) : Input
        data class Send(val event: ClientEvent) : Input
        data class Audio(val samples: ShortArray, val clientTimeMs: Long) : Input
        data class AckWaitOver(val generation: Int) : Input
        data class Pump(val generation: Int) : Input
        data class Settle(val generation: Int, val segmentIds: List<String>) : Input
    }

    private val inputs = Channel<Input>(Channel.UNLIMITED)

    // Loop-confined state.
    private var socket: SessionSocket? = null
    private var generation = 0
    private var handshaken = false
    private var mode: SttMode? = null
    private var attempt = 0
    private var stopped = false
    private var reconnectJob: Job? = null
    private var nextSeq = audio.lastSeq + 1

    /** Server mode: the highest `seq` sent on this socket (or known held by the backend). */
    private var sentThrough = -1L

    /** Server mode: false until resending may start (the `ack` after `hello.ack` came or timed out). */
    private var pumpOpen = false
    private var pumpScheduled = false

    /** The `audio_seq` of the last `ack` received on this socket, -1 before one. */
    private var socketAcked = -1L

    init {
        scope.launch(loopContext) {
            for (input in inputs) {
                handle(input)
                updateDrained()
            }
        }
    }

    /** Opens the first socket. */
    fun start() {
        inputs.trySend(Input.Start)
    }

    /** From [ConnectionState.Failed]: tries again at once. */
    fun retry() {
        inputs.trySend(Input.Retry)
    }

    /** Closes the socket for good; buffered messages are discarded. */
    fun stop() {
        inputs.trySend(Input.Stop)
    }

    /** Sends [event] now, or buffers it as the class doc describes. */
    fun send(event: ClientEvent) {
        inputs.trySend(Input.Send(event))
    }

    /** Sends one frame of PCM16 samples captured at [clientTimeMs] (server STT mode only). */
    fun sendAudio(samples: ShortArray, clientTimeMs: Long) {
        inputs.trySend(Input.Audio(samples, clientTimeMs))
    }

    private suspend fun handle(input: Input) {
        when (input) {
            Input.Start -> if (!stopped && socket == null && reconnectJob == null) open()
            Input.Retry -> if (!stopped && _state.value is ConnectionState.Failed) {
                attempt = 0
                open()
            }
            Input.Stop -> shutDown()
            is Input.Opened -> if (input.generation == generation) onOpened()
            is Input.Text -> if (input.generation == generation) onText(input.text)
            is Input.Closed -> if (input.generation == generation) onClosed(input.code, input.reason)
            is Input.Reconnect -> if (input.generation == generation && !stopped) {
                reconnectJob = null
                open()
            }
            is Input.Send -> onSend(input.event)
            is Input.Audio -> onAudio(input.samples, input.clientTimeMs)
            is Input.AckWaitOver -> if (input.generation == generation && handshaken && !pumpOpen) openPumpWithoutAck()
            is Input.Pump -> if (input.generation == generation) {
                pumpScheduled = false
                pump()
            }
            is Input.Settle -> if (input.generation == generation && handshaken) backlog.confirmFinals(input.segmentIds)
        }
    }

    private fun updateDrained() {
        val connected = handshaken && socket != null
        _drained.value = connected && backlog.queued().isEmpty() && when (mode) {
            SttMode.CLIENT -> backlog.finals().isEmpty()
            SttMode.SERVER -> !audio.hasUnacked
            null -> false
        }
    }

    private fun open() {
        if (stopped) return
        socket?.close()
        handshaken = false
        pumpOpen = false
        socketAcked = -1L
        val current = ++generation
        if (_state.value !is ConnectionState.Reconnecting) _state.value = ConnectionState.Connecting
        socket = socketFactory.open(
            url,
            token,
            object : SessionSocketListener {
                override fun onOpen() {
                    inputs.trySend(Input.Opened(current))
                }

                override fun onText(text: String) {
                    inputs.trySend(Input.Text(current, text))
                }

                override fun onClosed(code: Int, reason: String) {
                    inputs.trySend(Input.Closed(current, code, reason))
                }

                override fun onFailure(reason: String) {
                    inputs.trySend(Input.Closed(current, null, reason))
                }
            },
        )
    }

    private fun onOpened() {
        val hello = Hello(protocolVersion, capabilities, clock.nowMillis())
        socket?.sendText(encodeClientEvent(hello))
    }

    private fun onText(text: String) {
        val event = try {
            decodeServerEvent(text)
        } catch (e: IllegalArgumentException) {
            // SerializationException is an IllegalArgumentException: a contract violation.
            refuse("invalid server message: ${e.message}")
            return
        }
        if (!handshaken) {
            if (event !is HelloAck) {
                refuse("expected hello.ack, got ${event::class.simpleName}")
                return
            }
            onHelloAck(event)
        } else {
            when (event) {
                is TranscriptFinal -> backlog.confirmFinals(listOf(event.segmentId))
                is ServerAck -> event.audioSeq?.let(::onAudioAck)
                else -> Unit
            }
        }
        _events.tryEmit(event)
    }

    private fun onHelloAck(ack: HelloAck) {
        handshaken = true
        attempt = 0
        mode = ack.sttMode
        _state.value = ConnectionState.Connected(ack.sttMode, ack.clockOffsetMs)
        val socket = socket ?: return
        if (ack.sttMode == SttMode.CLIENT) {
            val finals = backlog.finals()
            for (final in finals) socket.sendText(encodeClientEvent(final))
            if (finals.isNotEmpty()) settleLater(finals.map { it.segmentId })
        }
        val queued = backlog.queued()
        for (event in queued) socket.sendText(encodeClientEvent(event))
        backlog.dequeue(queued.size)
        if (ack.sttMode == SttMode.SERVER) {
            sentThrough = audio.ackedSeq
            if (audio.hasUnacked) {
                // Learn where the backend stands before resending (its `ack` follows `hello.ack`).
                val current = generation
                scope.launch(loopContext) {
                    delay(ackWaitMs)
                    inputs.trySend(Input.AckWaitOver(current))
                }
            } else {
                openPump()
            }
        }
    }

    private fun settleLater(segmentIds: List<String>) {
        val current = generation
        scope.launch(loopContext) {
            delay(finalGraceMs)
            inputs.trySend(Input.Settle(current, segmentIds))
        }
    }

    private fun onAudioAck(acked: Long) {
        socketAcked = maxOf(socketAcked, acked)
        if (acked >= nextSeq) {
            // The backend already holds more frames than we produced (a resumed session after an
            // app restart without a spool): our held frames were dropped as repeats. Renumber them.
            rebase(acked)
            return
        }
        // Nothing sent past it on this socket yet: a gap after the backend's position (older audio
        // dropped at the cap) is closed by renumbering. Only a real `ack` ever triggers this.
        val unsentGap = sentThrough <= acked && firstHeldAfter(acked)?.let { it > acked + 1 } == true
        audio.acknowledge(acked)
        sentThrough = maxOf(sentThrough, acked)
        if (unsentGap && handshaken && mode == SttMode.SERVER) {
            rebase(acked)
            return
        }
        if (handshaken && mode == SttMode.SERVER && !pumpOpen) openPump()
    }

    private fun firstHeldAfter(seq: Long): Long? = audio.after(seq, 1).firstOrNull()?.seq

    /** Resending starts after the backend's `ack` on this socket (or when nothing is held). */
    private fun openPump() {
        pumpOpen = true
        sentThrough = maxOf(sentThrough, socketAcked, audio.ackedSeq)
        pump()
    }

    /**
     * No `ack` came within [ackWaitMs] of `hello.ack`. The backend sends one right behind
     * `hello.ack` whenever it holds audio, so either it holds none or the link is dying. Resend
     * without renumbering after the local ack, except when this backlog never saw an `ack` at all
     * and its frames do not start at 0 (older audio dropped at the cap before any was delivered):
     * then the backend holds nothing and the frames are renumbered from 0.
     */
    private fun openPumpWithoutAck() {
        val first = firstHeldAfter(-1)
        if (audio.ackedSeq < 0 && first != null && first > 0) {
            rebase(-1)
            return
        }
        openPump()
    }

    private fun rebase(acked: Long) {
        audio.rebaseAfter(acked)
        nextSeq = maxOf(audio.lastSeq, acked) + 1
        sentThrough = acked
        pumpOpen = true
        pump()
    }

    /** Sends held frames after [sentThrough] in order, a batch per turn, while the socket keeps up. */
    private fun pump() {
        if (!handshaken || mode != SttMode.SERVER || !pumpOpen || pumpScheduled) return
        val socket = socket ?: return
        val batch = audio.after(sentThrough, PUMP_BATCH)
        for (frame in batch) {
            if (socket.queuedBytes() > maxSocketQueueBytes) {
                schedulePump(PUMP_WAIT_MS)
                return
            }
            socket.sendBinary(encode(frame))
            sentThrough = frame.seq
        }
        if (batch.size == PUMP_BATCH) schedulePump(0)
    }

    private fun schedulePump(waitMs: Long) {
        pumpScheduled = true
        val current = generation
        if (waitMs == 0L) {
            inputs.trySend(Input.Pump(current))
        } else {
            scope.launch(loopContext) {
                delay(waitMs)
                inputs.trySend(Input.Pump(current))
            }
        }
    }

    private fun onSend(event: ClientEvent) {
        val connected = handshaken && socket != null
        when (event) {
            is TranscriptClientPartial -> if (connected && mode == SttMode.CLIENT) {
                socket?.sendText(encodeClientEvent(event))
            }
            is TranscriptClientFinal -> {
                backlog.putFinal(event)
                if (connected && mode == SttMode.CLIENT) socket?.sendText(encodeClientEvent(event))
            }
            else -> if (connected) {
                socket?.sendText(encodeClientEvent(event))
            } else {
                backlog.enqueue(event)
            }
        }
    }

    private fun onAudio(samples: ShortArray, clientTimeMs: Long) {
        val frame = SpooledFrame(nextSeq++, clientTimeMs, samples)
        audio.append(frame)
        val live = handshaken && mode == SttMode.SERVER && pumpOpen && !pumpScheduled && sentThrough == frame.seq - 1
        val socket = socket
        if (live && socket != null && socket.queuedBytes() <= maxSocketQueueBytes) {
            // Caught up: straight out (also when the backlog could not keep it, e.g. a full disk).
            socket.sendBinary(encode(frame))
            sentThrough = frame.seq
        } else {
            pump()
        }
    }

    private suspend fun onClosed(code: Int?, reason: String) {
        socket = null
        handshaken = false
        if (stopped) return
        when {
            code == CLOSE_UNKNOWN_SESSION -> {
                val resumed = try {
                    resume()
                } catch (e: Exception) {
                    if (e is CancellationException) throw e
                    false
                }
                if (stopped) return
                if (resumed) open() else _state.value = ConnectionState.Failed(ConnectionFailure.SessionNotActive)
            }
            code == null && reason.startsWith("HTTP 40") ->
                _state.value = ConnectionState.Failed(ConnectionFailure.Unauthorized(reason))
            code == CLOSE_POLICY_VIOLATION ->
                _state.value = ConnectionState.Failed(ConnectionFailure.Refused(reason))
            else -> scheduleReconnect(reason)
        }
    }

    private fun scheduleReconnect(reason: String) {
        val wait = reconnectDelaysMs[minOf(attempt, reconnectDelaysMs.lastIndex)]
        attempt++
        _state.value = ConnectionState.Reconnecting(attempt, reason)
        val current = generation
        reconnectJob = scope.launch {
            delay(wait)
            inputs.trySend(Input.Reconnect(current))
        }
    }

    private fun refuse(reason: String) {
        socket?.close()
        socket = null
        handshaken = false
        generation++ // ignore whatever the closed socket still reports
        _state.value = ConnectionState.Failed(ConnectionFailure.Refused(reason))
    }

    private fun shutDown() {
        stopped = true
        reconnectJob?.cancel()
        reconnectJob = null
        socket?.close()
        socket = null
        handshaken = false
        generation++
        _state.value = ConnectionState.Stopped
        inputs.close()
    }

    private fun encode(frame: SpooledFrame): ByteArray =
        AudioFrame(frame.seq, frame.clientTimeMs, frame.samples, protocolVersion).encode()

    companion object {
        /**
         * What the capture app offers in `hello`: client STT with Android's recognizer, or PCM16
         * streaming if the backend prefers server STT (ADR-0008).
         */
        val CAPTURE_CAPABILITIES: ClientCapabilities = ClientCapabilities(
            stt = SttMode.CLIENT,
            sttProvider = SpeechRecognizerTranscriber.PROVIDER_ID,
            audioFormat = AudioFormat(),
        )

        /** Back-off before each reconnect attempt; the last one repeats. */
        val DEFAULT_RECONNECT_DELAYS_MS: List<Long> = listOf(500, 1_000, 2_000, 5_000, 10_000)

        /** Buttons, markers and acks kept while disconnected. */
        const val MAX_QUEUED_EVENTS: Int = 100

        /** ~60 s of 100 ms frames kept until acknowledged. */
        const val MAX_BUFFERED_FRAMES: Int = 600

        const val MAX_UNCONFIRMED_FINALS: Int = 200

        /** How long to wait after `hello.ack` for the backend's `ack` before resending audio. */
        const val ACK_WAIT_MS: Long = 1_000

        /**
         * A resent final not echoed within this time of a connection that stayed up is held by the
         * backend (which drops a repeated final without echoing it). Longer than two ping intervals
         * of the session socket: OkHttp fails a socket whose pong is missing, so a socket still up
         * after this long proves the backend read what was sent before the pings (TCP keeps order).
         */
        const val FINAL_GRACE_MS: Long = 2 * SESSION_SOCKET_PING_INTERVAL_MS + 5_000

        /** Resending pauses while the socket has this much queued (OkHttp fails a send past 16 MiB). */
        const val MAX_SOCKET_QUEUE_BYTES: Long = 1L shl 20

        private const val PUMP_BATCH = 20
        private const val PUMP_WAIT_MS = 50L

        /** The backend's close code for a session that is not the active one. */
        const val CLOSE_UNKNOWN_SESSION: Int = 4404

        /** The backend's close code for a contract violation. */
        const val CLOSE_POLICY_VIOLATION: Int = 1008

        /** `ws_path` joined to the backend's base URL (OkHttp takes http(s) URLs for WebSockets). */
        fun socketUrl(baseUrl: String, wsPath: String): String =
            baseUrl.trimEnd('/') + "/" + wsPath.trimStart('/')
    }
}
