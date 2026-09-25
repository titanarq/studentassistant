package com.titanarq.studentassistant.tutor

import com.titanarq.studentassistant.capture.FakeRecognizerEngine
import com.titanarq.studentassistant.capture.RecognizerError
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceQuestionTest {
    private val engine = FakeRecognizerEngine()
    private val voice = VoiceQuestion(engine)
    private val events = mutableListOf<String>()
    private val listener = object : VoiceQuestion.Listener {
        override fun onInterim(text: String) {
            events += "interim:$text"
        }

        override fun onFinal(text: String) {
            events += "final:$text"
        }

        override fun onProblem(problem: VoiceProblem) {
            events += "problem:$problem"
        }
    }

    @Test
    fun `one round in Spanish reports the interim text, then the final one`() {
        voice.start(listener)
        assertTrue(voice.listening)
        assertEquals("es-ES", engine.lastLanguage)
        engine.listener.onPartial("qué es")
        engine.listener.onResult(" qué es la derivada ", 0.9)

        assertEquals(listOf("interim:qué es", "final:qué es la derivada"), events)
        assertFalse(voice.listening)
        assertEquals(1, engine.starts)
    }

    @Test
    fun `an empty result or an error keeps what was heard`() {
        voice.start(listener)
        engine.listener.onPartial("la integral")
        engine.listener.onResult("", null)
        voice.start(listener)
        engine.listener.onPartial("el límite")
        engine.listener.onError(RecognizerError.TRANSIENT)

        assertEquals(listOf("interim:la integral", "final:la integral", "interim:el límite", "final:el límite"), events)
    }

    @Test
    fun `silence and failures are problems`() {
        voice.start(listener)
        engine.listener.onError(RecognizerError.NO_SPEECH)
        voice.start(listener)
        engine.listener.onError(RecognizerError.PERMISSION_DENIED)
        voice.start(listener)
        engine.listener.onError(RecognizerError.UNAVAILABLE)
        voice.start(listener)
        engine.listener.onError(RecognizerError.BUSY)

        assertEquals(
            listOf("problem:NO_SPEECH", "problem:PERMISSION_DENIED", "problem:UNAVAILABLE", "problem:FAILED"),
            events,
        )
    }

    @Test
    fun `stop ends the round with what was heard and ignores the engine afterwards`() {
        voice.start(listener)
        val round = engine.listener
        round.onPartial("por qué")
        voice.stop()
        round.onResult("por qué no", null)
        round.onPartial("otra")

        assertEquals(listOf("interim:por qué", "final:por qué"), events)
        assertEquals(1, engine.cancels)
    }

    @Test
    fun `stop before anything was heard is no speech`() {
        voice.start(listener)
        voice.stop()
        assertEquals(listOf("problem:NO_SPEECH"), events)
    }

    @Test
    fun `cancel and release report nothing, and release frees the recognizer`() {
        voice.start(listener)
        val round = engine.listener
        voice.cancel()
        round.onResult("tarde", null)
        voice.start(listener)
        voice.release()

        assertEquals(emptyList<String>(), events)
        assertTrue(engine.destroyed)
        assertFalse(voice.listening)
    }

    @Test
    fun `a second start while listening is ignored`() {
        voice.start(listener)
        voice.start(listener)
        assertEquals(1, engine.starts)
    }
}
