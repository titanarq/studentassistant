@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import kotlinx.coroutines.ExperimentalCoroutinesApi
import com.titanarq.studentassistant.capture.ClientTranscript.Final
import com.titanarq.studentassistant.capture.ClientTranscript.Partial
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SpeechRecognizerTranscriberTest {
    private val clock = FakeClock(10_000)
    private val engine = FakeRecognizerEngine()
    private val emitted = mutableListOf<ClientTranscript>()
    private val errors = mutableListOf<TranscriberError>()

    private fun TestScope.transcriber() =
        SpeechRecognizerTranscriber(engine, clock, backgroundScope, segmentPrefix = "and-x")
            .also { it.start(onTranscript = { t -> emitted += t }, onError = { e -> errors += e }) }

    @Test
    fun `partials and the result of one round make one segment, then listening restarts`() = runTest {
        transcriber()
        assertEquals(1, engine.starts)
        assertEquals("es-ES", engine.lastLanguage)

        engine.listener.onSpeechStart()
        clock.now = 10_400
        engine.listener.onPartial("la edad")
        clock.now = 10_500
        engine.listener.onPartial("la edad ") // same text once trimmed: not repeated
        clock.now = 10_900
        engine.listener.onPartial("la edad media")
        clock.now = 11_500
        engine.listener.onResult("La Edad Media", 0.92)

        assertEquals(
            listOf(
                Partial("and-x-0", 10_000, 10_400, "la edad"),
                Partial("and-x-0", 10_000, 10_900, "la edad media"),
                Final("and-x-0", 10_000, 11_500, "La Edad Media", 0.92),
            ),
            emitted,
        )
        assertEquals(2, engine.starts)

        // The next utterance gets the next id and its own start time.
        clock.now = 12_000
        engine.listener.onPartial("empieza")
        assertEquals(Partial("and-x-1", 12_000, 12_000, "empieza"), emitted.last())
    }

    @Test
    fun `silence restarts at once and settles a dangling partial as the final`() = runTest {
        transcriber()
        engine.listener.onError(RecognizerError.NO_SPEECH)
        assertTrue(emitted.isEmpty())
        assertEquals(2, engine.starts)

        engine.listener.onPartial("feudal")
        clock.now = 10_300
        engine.listener.onError(RecognizerError.NO_SPEECH)
        assertEquals(Final("and-x-0", 10_000, 10_000, "feudal"), emitted.last())
        assertEquals(3, engine.starts)

        engine.listener.onResult("", null) // an empty result emits nothing
        assertEquals(2, emitted.size)
        assertEquals(4, engine.starts)
    }

    @Test
    fun `a failing recognizer is retried after a growing pause`() = runTest {
        transcriber()
        engine.listener.onError(RecognizerError.TRANSIENT)
        runCurrent()
        assertEquals(1, engine.starts)
        advanceTimeBy(249)
        runCurrent()
        assertEquals(1, engine.starts)
        advanceTimeBy(1)
        runCurrent()
        assertEquals(2, engine.starts)

        engine.listener.onError(RecognizerError.BUSY)
        advanceTimeBy(499)
        runCurrent()
        assertEquals(2, engine.starts)
        advanceTimeBy(1)
        runCurrent()
        assertEquals(3, engine.starts)

        // A result resets the pause.
        engine.listener.onResult("vale", null)
        engine.listener.onError(RecognizerError.TRANSIENT)
        advanceTimeBy(250)
        runCurrent()
        assertEquals(5, engine.starts)
    }

    @Test
    fun `a missing permission or recognizer stops and reports`() = runTest {
        transcriber()
        engine.listener.onError(RecognizerError.PERMISSION_DENIED)
        advanceTimeBy(10_000)
        runCurrent()
        assertEquals(listOf(TranscriberError.PERMISSION_DENIED), errors)
        assertEquals(1, engine.starts)
    }

    @Test
    fun `stop settles the utterance in progress, releases the recognizer and ignores late callbacks`() = runTest {
        val transcriber = transcriber()
        val round = engine.listener
        round.onPartial("última frase")
        transcriber.stop()
        assertEquals(Final("and-x-0", 10_000, 10_000, "última frase"), emitted.last())
        assertTrue(engine.destroyed)

        round.onResult("última frase dicha", null)
        round.onError(RecognizerError.NO_SPEECH)
        assertEquals(2, emitted.size)
        assertEquals(1, engine.starts)
    }

    @Test
    fun `a stopped transcriber can start again and the pending retry is cancelled`() = runTest {
        val transcriber = transcriber()
        engine.listener.onError(RecognizerError.TRANSIENT)
        transcriber.stop()
        advanceTimeBy(1_000)
        runCurrent()
        assertEquals(1, engine.starts)

        transcriber.start({ emitted += it }, { errors += it })
        assertEquals(2, engine.starts)
        assertFalse(errors.isNotEmpty())
    }

    @Test
    fun `android error codes map to recognizer errors`() {
        assertEquals(RecognizerError.NO_SPEECH, AndroidSpeechRecognizerEngine.mapError(7)) // NO_MATCH
        assertEquals(RecognizerError.NO_SPEECH, AndroidSpeechRecognizerEngine.mapError(6)) // SPEECH_TIMEOUT
        assertEquals(RecognizerError.BUSY, AndroidSpeechRecognizerEngine.mapError(8))
        assertEquals(RecognizerError.PERMISSION_DENIED, AndroidSpeechRecognizerEngine.mapError(9))
        assertEquals(RecognizerError.UNAVAILABLE, AndroidSpeechRecognizerEngine.mapError(12)) // LANGUAGE_NOT_SUPPORTED
        assertEquals(RecognizerError.TRANSIENT, AndroidSpeechRecognizerEngine.mapError(2)) // NETWORK
    }
}
