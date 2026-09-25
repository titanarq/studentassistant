package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.Clock
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
 * Reconnect on drop, with in-memory buffers only (the disk spool is android-offline):
 * - transcript finals stay buffered until the backend echoes the `transcript.final` with the same
 *   `segment_id` and are resent after every reconnect (the backend drops a final it saw already);
 * - `button`, `marker` and `ack` sent while disconnected are queued and sent after `hello.ack`;
 *   partials are dropped while disconnected (their final supersedes them);
 * - audio frames stay buffered until the server `ack`'s `audio_seq` covers them and are resent
 *   after every `hello.ack` in server mode (the backend drops a `seq` it already has). When the
 *   backend already acknowledges more frames than this connection produced (a restarted app on a
 *   resumed session), the buffered frames are renumbered after that `seq` and resent.
 *
 * A close with code 4404 (the session is not active, e.g. the backend restarted) runs [resume]
 * (the REST resume) and reconnects when it returns true.
 *
 * Everything runs on one coroutine of [scope] fed by a channel, so socket callbacks (any thread)
 * and callers (any thread) never race. [start] opens the first socket.
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
    private val maxQueuedEvents: Int = MAX_QUEUED_EVENTS,
    private val maxBufferedFrames: Int = MAX_BUFFERED_FRAMES,
    private val maxUnconfirmedFinals: Int = MAX_UNCONFIRMED_FINALS,
    private val protocolVersion: String = PROTOCOL_VERSION,
) {
    private val _state = MutableStateFlow<ConnectionState>(ConnectionState.Connecting)
    val state: StateFlow<ConnectionState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<ServerEvent>(extraBufferCapacity = 256)

    /** Every server event after `hello.ack` (and `hello.ack` itself), in arrival order. */
    val events: SharedFlow<ServerEvent> = _events.asSharedFlow()

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
    }

    private class BufferedFrame(var seq: Long, val clientTimeMs: Long, val samples: ShortArray)

    private val inputs = Channel<Input>(Channel.UNLIMITED)

    // Loop-confined state.
    private var socket: SessionSocket? = null
    private var generation = 0
    private var handshaken = false
    private var mode: SttMode? = null
    private var attempt = 0
    private var stopped = false
    private var reconnectJob: Job? = null
    private val queued = ArrayDeque<ClientEvent>()
    private val unconfirmedFinals = LinkedHashMap<String, TranscriptClientFinal>()
    private val frames = ArrayDeque<BufferedFrame>()
    private var nextSeq = 0L

    init {
        scope.launch {
            for (input in inputs) handle(input)
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
        }
    }

    private fun open() {
        if (stopped) return
        socket?.close()
        handshaken = false
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
                is TranscriptFinal -> unconfirmedFinals.remove(event.segmentId)
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
            for (final in unconfirmedFinals.values) socket.sendText(encodeClientEvent(final))
        }
        while (queued.isNotEmpty()) socket.sendText(encodeClientEvent(queued.removeFirst()))
        if (ack.sttMode == SttMode.SERVER) {
            for (frame in frames) socket.sendBinary(encode(frame))
        }
    }

    private fun onAudioAck(acked: Long) {
        if (acked >= nextSeq) {
            // The backend already holds more frames than we produced (a resumed session after an
            // app restart): our buffered frames were dropped as repeats. Renumber and resend them.
            nextSeq = acked + 1
            for (frame in frames) {
                frame.seq = nextSeq++
                socket?.sendBinary(encode(frame))
            }
            return
        }
        while (frames.isNotEmpty() && frames.first().seq <= acked) frames.removeFirst()
    }

    private fun onSend(event: ClientEvent) {
        val connected = handshaken && socket != null
        when (event) {
            is TranscriptClientPartial -> if (connected && mode == SttMode.CLIENT) {
                socket?.sendText(encodeClientEvent(event))
            }
            is TranscriptClientFinal -> {
                unconfirmedFinals[event.segmentId] = event
                while (unconfirmedFinals.size > maxUnconfirmedFinals) {
                    unconfirmedFinals.remove(unconfirmedFinals.keys.first())
                }
                if (connected && mode == SttMode.CLIENT) socket?.sendText(encodeClientEvent(event))
            }
            else -> if (connected) {
                socket?.sendText(encodeClientEvent(event))
            } else {
                queued.addLast(event)
                while (queued.size > maxQueuedEvents) queued.removeFirst()
            }
        }
    }

    private fun onAudio(samples: ShortArray, clientTimeMs: Long) {
        val frame = BufferedFrame(nextSeq++, clientTimeMs, samples)
        frames.addLast(frame)
        while (frames.size > maxBufferedFrames) frames.removeFirst()
        if (handshaken && mode == SttMode.SERVER) socket?.sendBinary(encode(frame))
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
        generation++
        queued.clear()
        unconfirmedFinals.clear()
        frames.clear()
        _state.value = ConnectionState.Stopped
        inputs.close()
    }

    private fun encode(frame: BufferedFrame): ByteArray =
        AudioFrame(frame.seq, frame.clientTimeMs, frame.samples, protocolVersion).encode()

    companion object {
        /** Back-off before each reconnect attempt; the last one repeats. */
        val DEFAULT_RECONNECT_DELAYS_MS: List<Long> = listOf(500, 1_000, 2_000, 5_000, 10_000)

        /** Buttons, markers and acks kept while disconnected. */
        const val MAX_QUEUED_EVENTS: Int = 100

        /** ~60 s of 100 ms frames kept until acknowledged. */
        const val MAX_BUFFERED_FRAMES: Int = 600

        const val MAX_UNCONFIRMED_FINALS: Int = 200

        /** The backend's close code for a session that is not the active one. */
        const val CLOSE_UNKNOWN_SESSION: Int = 4404

        /** The backend's close code for a contract violation. */
        const val CLOSE_POLICY_VIOLATION: Int = 1008

        /** `ws_path` joined to the backend's base URL (OkHttp takes http(s) URLs for WebSockets). */
        fun socketUrl(baseUrl: String, wsPath: String): String =
            baseUrl.trimEnd('/') + "/" + wsPath.trimStart('/')
    }
}
