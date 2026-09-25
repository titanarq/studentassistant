package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.protocol.AudioFrame
import com.titanarq.studentassistant.protocol.ClientEvent
import com.titanarq.studentassistant.protocol.ServerEvent
import com.titanarq.studentassistant.protocol.decodeClientEvent
import com.titanarq.studentassistant.protocol.encodeServerEvent

/** A settable clock. */
class FakeClock(var now: Long = 1_000_000L) : Clock {
    override fun nowMillis(): Long = now
}

/** A scripted socket: the test plays the backend through [open], [receive], [drop], [closeByServer]. */
class FakeSessionSocket(
    val url: String,
    val token: String,
    private val listener: SessionSocketListener,
) : SessionSocket {
    val texts = mutableListOf<String>()
    val binaries = mutableListOf<ByteArray>()
    var closed = false
        private set

    /** Every text message the client sent, decoded. */
    val sent: List<ClientEvent> get() = texts.map(::decodeClientEvent)

    /** Every audio frame the client sent, decoded. */
    val frames: List<AudioFrame> get() = binaries.map { AudioFrame.decode(it) }

    override fun sendText(text: String): Boolean {
        check(!closed) { "send on a closed socket" }
        texts += text
        return true
    }

    override fun sendBinary(bytes: ByteArray): Boolean {
        check(!closed) { "send on a closed socket" }
        binaries += bytes
        return true
    }

    override fun close() {
        closed = true
    }

    fun open() = listener.onOpen()

    fun receive(event: ServerEvent) = listener.onText(encodeServerEvent(event))

    fun receiveText(text: String) = listener.onText(text)

    fun drop(reason: String = "connection reset") = listener.onFailure(reason)

    fun closeByServer(code: Int, reason: String) = listener.onClosed(code, reason)
}

class FakeSessionSocketFactory : SessionSocketFactory {
    val sockets = mutableListOf<FakeSessionSocket>()

    val last: FakeSessionSocket get() = sockets.last()

    override fun open(url: String, token: String, listener: SessionSocketListener): SessionSocket =
        FakeSessionSocket(url, token, listener).also { sockets += it }
}

/** A recognizer the test drives through [listener]. */
class FakeRecognizerEngine : RecognizerEngine {
    var starts = 0
        private set
    var cancels = 0
        private set
    var destroyed = false
        private set
    var lastLanguage: String? = null
        private set
    private var current: RecognizerListener? = null

    val listener: RecognizerListener get() = checkNotNull(current) { "not listening" }

    override fun startListening(language: String, listener: RecognizerListener) {
        starts++
        lastLanguage = language
        current = listener
    }

    override fun cancel() {
        cancels++
    }

    override fun destroy() {
        destroyed = true
    }
}

/** A transcriber the test drives through [emit] and [fail]. */
class FakeTranscriber(
    override val providerId: String = "android-speech",
    override val language: String = "es-ES",
) : ClientTranscriber {
    var running = false
        private set
    var starts = 0
        private set
    private var onTranscript: ((ClientTranscript) -> Unit)? = null
    private var onError: ((TranscriberError) -> Unit)? = null

    override fun start(onTranscript: (ClientTranscript) -> Unit, onError: (TranscriberError) -> Unit) {
        running = true
        starts++
        this.onTranscript = onTranscript
        this.onError = onError
    }

    override fun stop() {
        running = false
    }

    fun emit(transcript: ClientTranscript) = checkNotNull(onTranscript).invoke(transcript)

    fun fail(error: TranscriberError) = checkNotNull(onError).invoke(error)
}

/** Serves [frames] frames of [samplesPerRead]-sample reads (sample values count up), then fails. */
class FakeAudioSource(
    private val totalSamples: Int,
    private val samplesPerRead: Int = 400,
    private val canOpen: Boolean = true,
    override val sampleRateHz: Int = 16_000,
) : AudioSource {
    var opened = false
        private set
    var closed = false
        private set
    private var served = 0

    override fun open(): Boolean {
        opened = canOpen
        return canOpen
    }

    override fun read(buffer: ShortArray, offset: Int, length: Int): Int {
        if (served >= totalSamples) return -1
        val count = minOf(length, samplesPerRead, totalSamples - served)
        for (i in 0 until count) buffer[offset + i] = (served + i).toShort()
        served += count
        return count
    }

    override fun close() {
        closed = true
    }
}
