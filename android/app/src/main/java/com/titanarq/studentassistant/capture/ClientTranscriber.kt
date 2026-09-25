package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.Clock
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** One recognised piece of speech, on the client's clock; the final replaces its partials. */
sealed interface ClientTranscript {
    /** Shared by the partials and the final of one utterance; unique within the session. */
    val segmentId: String
    val clientStartMs: Long
    val clientEndMs: Long
    val text: String
    val confidence: Double?

    data class Partial(
        override val segmentId: String,
        override val clientStartMs: Long,
        override val clientEndMs: Long,
        override val text: String,
        override val confidence: Double? = null,
    ) : ClientTranscript

    data class Final(
        override val segmentId: String,
        override val clientStartMs: Long,
        override val clientEndMs: Long,
        override val text: String,
        override val confidence: Double? = null,
    ) : ClientTranscript
}

/** Why a [ClientTranscriber] stopped on its own. */
enum class TranscriberError {
    /** The microphone permission is missing. */
    PERMISSION_DENIED,

    /** No recognizer on the device, or it cannot handle the language. */
    UNAVAILABLE,
}

/**
 * Client-side speech recognition (ADR-0008): the client's pluggable recognizer, the counterpart of
 * the backend's `SpeechToTextProvider`. [start] listens continuously until [stop].
 */
interface ClientTranscriber {
    /** The `provider` / `stt_provider` id sent to the backend, e.g. `android-speech`. */
    val providerId: String

    /** BCP 47 language, e.g. `es-ES`. */
    val language: String

    /**
     * The session's domain terms (protocol 1.4 `vocabulary_hints`, most important first) the
     * recognizer is biased towards where the platform supports it; a change applies from the next
     * listening round. Empty: no biasing.
     */
    var vocabularyHints: List<String>

    /** Callbacks arrive on the thread the transcriber was built for (the main thread on Android). */
    fun start(onTranscript: (ClientTranscript) -> Unit, onError: (TranscriberError) -> Unit)

    /** Stops listening; an utterance still in progress is settled as a final first. */
    fun stop()
}

/** What a platform recognizer reports for one listening round. */
interface RecognizerListener {
    fun onSpeechStart()

    fun onPartial(text: String)

    /** The round's best hypothesis ([text] may be empty) and its confidence in [0, 1], if known. */
    fun onResult(text: String, confidence: Double?)

    fun onError(error: RecognizerError)
}

enum class RecognizerError {
    /** Silence or nothing recognisable: an ordinary end of a round. */
    NO_SPEECH,

    /** The recognizer is still busy with the previous round. */
    BUSY,

    /** Network, server, audio or client error; worth retrying after a pause. */
    TRANSIENT,

    PERMISSION_DENIED,

    UNAVAILABLE,
}

/**
 * One utterance-long listening round of a platform recognizer (Android's `SpeechRecognizer`
 * behind [AndroidSpeechRecognizerEngine]); [SpeechRecognizerTranscriber] chains the rounds.
 */
interface RecognizerEngine {
    /**
     * Starts a round in [language], biased towards [vocabularyHints] where the platform supports
     * it (ignored otherwise).
     */
    fun startListening(language: String, vocabularyHints: List<String>, listener: RecognizerListener)

    /** Abandons the current round. */
    fun cancel()

    /** Releases the recognizer; a later [startListening] creates a new one. */
    fun destroy()
}

/**
 * [ClientTranscriber] over a [RecognizerEngine]: restarts a round as soon as one ends (a result,
 * silence) so recognition is continuous, and after [retryDelaysMs] when the recognizer fails;
 * turns each round into `Partial`s and one `Final` with client timestamps (start at the first sign
 * of speech, end at the latest hypothesis). A round that ends in an error after partials is
 * settled with its last partial as the final, so no utterance is left dangling.
 *
 * Not thread-safe: the engine's callbacks and every call must come from [scope]'s thread.
 */
class SpeechRecognizerTranscriber(
    private val engine: RecognizerEngine,
    private val clock: Clock,
    private val scope: CoroutineScope,
    override val language: String = "es-ES",
    override val providerId: String = PROVIDER_ID,
    /** Makes segment ids unique across app restarts within one session. */
    private val segmentPrefix: String = "and-" + UUID.randomUUID().toString().take(8),
    private val retryDelaysMs: List<Long> = listOf(250, 500, 1_000, 2_000, 5_000),
) : ClientTranscriber {
    override var vocabularyHints: List<String> = emptyList()

    private var onTranscript: ((ClientTranscript) -> Unit)? = null
    private var onError: ((TranscriberError) -> Unit)? = null
    private var running = false
    private var round = 0
    private var counter = 0
    private var failures = 0
    private var utteranceStart: Long? = null
    private var lastPartial: ClientTranscript.Partial? = null
    private var retryJob: Job? = null

    override fun start(onTranscript: (ClientTranscript) -> Unit, onError: (TranscriberError) -> Unit) {
        if (running) return
        this.onTranscript = onTranscript
        this.onError = onError
        running = true
        failures = 0
        listen()
    }

    override fun stop() {
        if (!running) return
        running = false
        retryJob?.cancel()
        retryJob = null
        round++ // late callbacks of the cancelled round are ignored
        settlePartial()
        engine.cancel()
        engine.destroy()
    }

    private fun segmentId(): String = "$segmentPrefix-$counter"

    private fun listen() {
        utteranceStart = null
        lastPartial = null
        val current = ++round
        engine.startListening(
            language,
            vocabularyHints,
            object : RecognizerListener {
                override fun onSpeechStart() {
                    if (current == round && utteranceStart == null) utteranceStart = clock.nowMillis()
                }

                override fun onPartial(text: String) {
                    if (current == round) partial(text)
                }

                override fun onResult(text: String, confidence: Double?) {
                    if (current == round) result(text, confidence)
                }

                override fun onError(error: RecognizerError) {
                    if (current == round) failed(error)
                }
            },
        )
    }

    private fun partial(text: String) {
        val trimmed = text.trim()
        if (trimmed.isEmpty() || trimmed == lastPartial?.text) return
        val now = clock.nowMillis()
        val start = utteranceStart ?: now.also { utteranceStart = it }
        val partial = ClientTranscript.Partial(segmentId(), start, maxOf(start, now), trimmed)
        lastPartial = partial
        onTranscript?.invoke(partial)
    }

    private fun result(text: String, confidence: Double?) {
        failures = 0
        val trimmed = text.trim()
        if (trimmed.isEmpty()) {
            settlePartial()
        } else {
            val now = clock.nowMillis()
            val start = utteranceStart ?: now
            emitFinal(ClientTranscript.Final(segmentId(), start, maxOf(start, now), trimmed, confidence))
        }
        listen()
    }

    private fun failed(error: RecognizerError) {
        settlePartial()
        when (error) {
            RecognizerError.NO_SPEECH -> {
                failures = 0
                listen()
            }
            RecognizerError.BUSY, RecognizerError.TRANSIENT -> {
                val wait = retryDelaysMs[minOf(failures, retryDelaysMs.lastIndex)]
                failures++
                round++
                engine.cancel()
                retryJob = scope.launch {
                    delay(wait)
                    retryJob = null
                    if (running) listen()
                }
            }
            RecognizerError.PERMISSION_DENIED, RecognizerError.UNAVAILABLE -> {
                running = false
                round++
                engine.cancel()
                onError?.invoke(
                    if (error == RecognizerError.PERMISSION_DENIED) {
                        TranscriberError.PERMISSION_DENIED
                    } else {
                        TranscriberError.UNAVAILABLE
                    },
                )
            }
        }
    }

    /** The round ended without a final: its last partial, if any, becomes the final. */
    private fun settlePartial() {
        val partial = lastPartial ?: return
        emitFinal(ClientTranscript.Final(partial.segmentId, partial.clientStartMs, partial.clientEndMs, partial.text))
    }

    private fun emitFinal(final: ClientTranscript.Final) {
        lastPartial = null
        utteranceStart = null
        counter++
        onTranscript?.invoke(final)
    }

    companion object {
        const val PROVIDER_ID: String = "android-speech"
    }
}
