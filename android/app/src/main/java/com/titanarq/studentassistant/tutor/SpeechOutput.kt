package com.titanarq.studentassistant.tutor

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/** Reads one text at a time aloud: [speak] replaces whatever is being read. */
interface SpeechOutput {
    /** False once the phone turned out to have no Spanish synthesis; answers are then only written. */
    val available: Boolean

    /** Reads [text]; [onDone] runs once, on any thread, when it is done, replaced, stopped or failed. */
    fun speak(text: String, onDone: () -> Unit)

    /** Stops reading at once. */
    fun stop()

    /** Releases the synthesizer. */
    fun shutdown()
}

/** A [SpeechOutput] that reads nothing (the container's default, and a phone without synthesis). */
object NoSpeechOutput : SpeechOutput {
    override val available: Boolean = false

    override fun speak(text: String, onDone: () -> Unit) = onDone()

    override fun stop() = Unit

    override fun shutdown() = Unit
}

/**
 * [SpeechOutput] over Android's [TextToSpeech] in Spanish (`es-ES`). The engine starts
 * asynchronously: a text asked for before it is ready waits for it; an engine that fails to start,
 * or has no Spanish, makes [available] false and every [speak] ends at once. Texts longer than the
 * engine's input limit are read as several utterances ([speechChunks]). Main thread only.
 */
class AndroidSpeechOutput(context: Context, private val locale: Locale = Locale.forLanguageTag(VoiceQuestion.LANGUAGE)) : SpeechOutput {
    private var ready = false
    private var failed = false
    private var pending: Pair<String, () -> Unit>? = null
    private val callbacks = ConcurrentHashMap<String, () -> Unit>()
    private val ids = AtomicInteger()
    private val tts: TextToSpeech = TextToSpeech(context.applicationContext) { status -> onInit(status) }

    init {
        tts.setOnUtteranceProgressListener(
            object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) = Unit

                override fun onDone(utteranceId: String?) = finished(utteranceId)

                @Deprecated("Deprecated in Java")
                override fun onError(utteranceId: String?) = finished(utteranceId)

                override fun onError(utteranceId: String?, errorCode: Int) = finished(utteranceId)

                override fun onStop(utteranceId: String?, interrupted: Boolean) = finished(utteranceId)
            },
        )
    }

    override val available: Boolean get() = !failed

    override fun speak(text: String, onDone: () -> Unit) {
        stop()
        when {
            failed -> onDone()
            !ready -> pending = text to onDone
            else -> read(text, onDone)
        }
    }

    override fun stop() {
        pending?.second?.invoke()
        pending = null
        if (ready) tts.stop()
        // Every utterance still queued is over; the engine's own onStop then finds nothing.
        callbacks.keys.toList().forEach { finished(it) }
    }

    override fun shutdown() {
        stop()
        tts.shutdown()
    }

    private fun onInit(status: Int) {
        val language = if (status == TextToSpeech.SUCCESS) tts.setLanguage(locale) else TextToSpeech.LANG_NOT_SUPPORTED
        ready = status == TextToSpeech.SUCCESS && language >= TextToSpeech.LANG_AVAILABLE
        failed = !ready
        val waiting = pending
        pending = null
        if (waiting != null) {
            if (ready) read(waiting.first, waiting.second) else waiting.second()
        }
    }

    private fun read(text: String, onDone: () -> Unit) {
        val chunks = speechChunks(text, TextToSpeech.getMaxSpeechInputLength())
        if (chunks.isEmpty()) {
            onDone()
            return
        }
        // Only the last utterance reports the end; a queue flushed early reports it through onStop.
        val first = ids.getAndAdd(chunks.size)
        val lastId = "tutor-${first + chunks.lastIndex}"
        callbacks[lastId] = onDone
        chunks.forEachIndexed { index, chunk ->
            val mode = if (index == 0) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD
            if (tts.speak(chunk, mode, null, "tutor-${first + index}") != TextToSpeech.SUCCESS) {
                tts.stop()
                finished(lastId)
                return
            }
        }
    }

    private fun finished(utteranceId: String?) {
        if (utteranceId != null) callbacks.remove(utteranceId)?.invoke()
    }
}
