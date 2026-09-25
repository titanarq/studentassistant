package com.titanarq.studentassistant.capture

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * [RecognizerEngine] over Android's [SpeechRecognizer] (Google on most phones, ADR-0008): free-form
 * dictation in the requested language with partial results, preferring the offline model when the
 * device has one. Must be used from the main thread, as [SpeechRecognizer] requires.
 */
class AndroidSpeechRecognizerEngine(private val context: Context) : RecognizerEngine {
    private var recognizer: SpeechRecognizer? = null

    override fun startListening(language: String, listener: RecognizerListener) {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            listener.onError(RecognizerError.UNAVAILABLE)
            return
        }
        val recognizer = recognizer ?: SpeechRecognizer.createSpeechRecognizer(context).also { recognizer = it }
        recognizer.setRecognitionListener(Adapter(listener))
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, language)
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
        recognizer.startListening(intent)
    }

    override fun cancel() {
        recognizer?.cancel()
    }

    override fun destroy() {
        recognizer?.destroy()
        recognizer = null
    }

    private class Adapter(private val listener: RecognizerListener) : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = Unit

        override fun onBeginningOfSpeech() = listener.onSpeechStart()

        override fun onRmsChanged(rmsdB: Float) = Unit

        override fun onBufferReceived(buffer: ByteArray?) = Unit

        override fun onEndOfSpeech() = Unit

        override fun onError(error: Int) = listener.onError(mapError(error))

        override fun onResults(results: Bundle?) {
            val texts = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            val scores = results?.getFloatArray(SpeechRecognizer.CONFIDENCE_SCORES)
            // 0 and -1 mean "not available"; anything outside (0, 1] is left out of the protocol.
            val confidence = scores?.firstOrNull()?.toDouble()?.takeIf { it > 0.0 && it <= 1.0 }
            listener.onResult(texts?.firstOrNull().orEmpty(), confidence)
        }

        override fun onPartialResults(partialResults: Bundle?) {
            val text = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
            if (!text.isNullOrBlank()) listener.onPartial(text)
        }

        override fun onEvent(eventType: Int, params: Bundle?) = Unit
    }

    companion object {
        fun mapError(error: Int): RecognizerError = when (error) {
            SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> RecognizerError.NO_SPEECH
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> RecognizerError.BUSY
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> RecognizerError.PERMISSION_DENIED
            SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED, SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE ->
                RecognizerError.UNAVAILABLE
            else -> RecognizerError.TRANSIENT
        }
    }
}
