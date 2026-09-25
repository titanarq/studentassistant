package com.titanarq.studentassistant.tutor

import com.titanarq.studentassistant.capture.RecognizerEngine
import com.titanarq.studentassistant.capture.RecognizerError
import com.titanarq.studentassistant.capture.RecognizerListener

/** Why a spoken question gave no text. */
enum class VoiceProblem {
    /** Silence, or nothing recognisable. */
    NO_SPEECH,

    /** The microphone permission is missing. */
    PERMISSION_DENIED,

    /** No recognizer on the phone, or none for Spanish. */
    UNAVAILABLE,

    /** Network, server, audio or a busy recognizer. */
    FAILED,
}

/**
 * One spoken question (#248): a single listening round of a [RecognizerEngine] (Android's
 * `SpeechRecognizer` in `es-ES`, the capture screen's engine) with its interim text, which ends when
 * the student stops talking and hands over the recognised text. [stop] ends it early and keeps what
 * was heard so far. Unlike the capture screen's transcriber it never restarts and never talks to a
 * session.
 *
 * Not thread-safe: every call and the engine's callbacks come from the main thread.
 */
class VoiceQuestion(
    private val engine: RecognizerEngine,
    private val language: String = LANGUAGE,
) {
    /** What a listening round reports; exactly one of [onFinal] / [onProblem] ends it. */
    interface Listener {
        /** The question so far, while the student speaks. */
        fun onInterim(text: String)

        /** The recognised question (never blank). */
        fun onFinal(text: String)

        fun onProblem(problem: VoiceProblem)
    }

    /** True from [start] until the round ends. */
    var listening: Boolean = false
        private set

    private var round = 0
    private var heard = ""
    private var listener: Listener? = null

    /** Starts listening for one question; ignored while a round is running. */
    fun start(listener: Listener) {
        if (listening) return
        listening = true
        heard = ""
        this.listener = listener
        val current = ++round
        engine.startListening(
            language,
            emptyList(),
            object : RecognizerListener {
                override fun onSpeechStart() = Unit

                override fun onPartial(text: String) {
                    if (current != round || !listening || text.isBlank()) return
                    heard = text.trim()
                    listener.onInterim(heard)
                }

                override fun onResult(text: String, confidence: Double?) {
                    if (current == round) settle(text.trim().ifEmpty { heard })
                }

                override fun onError(error: RecognizerError) {
                    if (current != round) return
                    if (heard.isNotEmpty()) settle(heard) else end { it.onProblem(problemOf(error)) }
                }
            },
        )
    }

    /** Ends the round now, keeping what was heard (nothing heard: [VoiceProblem.NO_SPEECH]). */
    fun stop() {
        if (!listening) return
        engine.cancel()
        settle(heard)
    }

    /** Abandons the round without reporting anything. */
    fun cancel() {
        if (!listening) return
        engine.cancel()
        listening = false
        listener = null
        round++
    }

    /** Releases the recognizer (the screen's view model is gone). */
    fun release() {
        cancel()
        engine.destroy()
    }

    private fun settle(text: String) {
        if (text.isBlank()) end { it.onProblem(VoiceProblem.NO_SPEECH) } else end { it.onFinal(text) }
    }

    private fun end(report: (Listener) -> Unit) {
        if (!listening) return
        val current = listener
        listening = false
        listener = null
        round++
        current?.let(report)
    }

    companion object {
        /** The language questions are recognised in (and answers read aloud in). */
        const val LANGUAGE = "es-ES"

        fun problemOf(error: RecognizerError): VoiceProblem = when (error) {
            RecognizerError.NO_SPEECH -> VoiceProblem.NO_SPEECH
            RecognizerError.PERMISSION_DENIED -> VoiceProblem.PERMISSION_DENIED
            RecognizerError.UNAVAILABLE -> VoiceProblem.UNAVAILABLE
            RecognizerError.BUSY, RecognizerError.TRANSIENT -> VoiceProblem.FAILED
        }
    }
}
