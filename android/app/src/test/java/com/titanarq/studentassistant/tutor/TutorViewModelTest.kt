package com.titanarq.studentassistant.tutor

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.PairedBackend
import com.titanarq.studentassistant.capture.FakeRecognizerEngine
import com.titanarq.studentassistant.capture.RecognizerError
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class TutorViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    // The store runs on the test thread (see ConnectionTestViewModelTest).
    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val topic = TutorTopic("calculo", "derivadas", "Derivadas")
    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "Portátil")
    private val client = FakeTutorClient()
    private val engine = FakeRecognizerEngine()
    private val speech = FakeSpeechOutput()

    private val earlier = TutorTurn("2026-09-24T10:00:00Z", "¿Qué es?", "Un límite[^p1].", listOf(TutorRef("p1", "notes", "Apuntes, página 1")))
    private val answer = TutorAnswer(
        question = "¿Y la regla de la cadena?",
        reply = "Se derivan las funciones **una dentro de otra**[^p2].",
        refs = listOf(TutorRef("p2", "book", "Libro, página 40")),
    )

    @After
    fun tearDown() {
        scope.cancel()
    }

    private fun viewModel(paired: Boolean = true): TutorViewModel {
        if (paired) runBlocking { store.save(home) }
        val viewModel = TutorViewModel(client, store, topic, VoiceQuestion(engine), speech)
        runBlocking { withTimeout(5_000) { viewModel.state.first { it.setup != TutorSetup.Loading } } }
        return viewModel
    }

    private val TutorViewModel.now: TutorUiState get() = state.value

    @Test
    fun `the topic's earlier questions are loaded from the active backend`() {
        client.historyResult = TutorResult.Success(listOf(earlier))
        val viewModel = viewModel()

        assertEquals(TutorSetup.Ready("Portátil"), viewModel.now.setup)
        assertEquals(TutorHistoryState.Loaded, viewModel.now.history)
        assertEquals(listOf(earlier), viewModel.now.turns)
        assertEquals(listOf(BackendCredentials(home.baseUrl, "sa_tok")), client.historyCalls)
        assertTrue(viewModel.now.canAsk)
    }

    @Test
    fun `without a paired backend nothing is called and nothing can be asked`() {
        val viewModel = viewModel(paired = false)

        assertEquals(TutorSetup.NoBackend, viewModel.now.setup)
        assertFalse(viewModel.now.canAsk)
        viewModel.onDraftChange("¿Algo?")
        viewModel.askDraft()
        assertTrue(client.historyCalls.isEmpty())
        assertTrue(client.asks.isEmpty())
    }

    @Test
    fun `a failed history can be retried`() {
        client.historyResult = TutorResult.Unreachable("ConnectException")
        val viewModel = viewModel()
        assertEquals(TutorHistoryState.Failed(TutorResult.Unreachable("ConnectException")), viewModel.now.history)

        client.historyResult = TutorResult.Success(listOf(earlier))
        viewModel.retryHistory()

        assertEquals(TutorHistoryState.Loaded, viewModel.now.history)
        assertEquals(listOf(earlier), viewModel.now.turns)
    }

    @Test
    fun `a typed question streams its answer, adds the turn and reads it aloud without marks`() {
        val viewModel = viewModel()
        viewModel.onDraftChange("  ¿Y la regla de la cadena?  ")
        viewModel.askDraft()

        val ask = client.asks.single()
        assertEquals("¿Y la regla de la cadena?", ask.question)
        assertEquals("calculo", ask.subjectId)
        assertEquals("derivadas", ask.topicId)
        assertFalse(ask.confirmOverCap)
        assertEquals("", viewModel.now.draft)
        assertEquals(PendingQuestion("¿Y la regla de la cadena?"), viewModel.now.pending)
        assertFalse(viewModel.now.canAsk)

        ask.progress(TutorProgress.Delta("Se derivan "))
        ask.progress(TutorProgress.Delta("las funciones"))
        assertEquals("Se derivan las funciones", viewModel.now.pending?.partial)
        ask.progress(TutorProgress.Restart)
        assertEquals("", viewModel.now.pending?.partial)

        ask.reply.complete(TutorResult.Success(answer))

        assertNull(viewModel.now.pending)
        val turn = viewModel.now.turns.single()
        assertEquals(answer.question, turn.question)
        assertEquals(answer.reply, turn.reply)
        assertEquals(answer.refs, turn.refs)
        assertEquals(listOf("Se derivan las funciones una dentro de otra."), speech.spoken)
        assertTrue(viewModel.now.speaking)

        speech.finish()
        assertFalse(viewModel.now.speaking)
    }

    @Test
    fun `with reading aloud off the answer is only shown`() {
        val viewModel = viewModel()
        viewModel.setReadAloud(false)
        viewModel.onDraftChange("¿Qué?")
        viewModel.askDraft()
        client.asks.single().reply.complete(TutorResult.Success(answer))

        assertTrue(speech.spoken.isEmpty())
        assertEquals(1, viewModel.now.turns.size)
    }

    @Test
    fun `stop reading stops the voice, and a stale end does not touch a newer reading`() {
        client.historyResult = TutorResult.Success(listOf(earlier))
        val viewModel = viewModel()
        viewModel.readAgain(earlier)
        assertEquals(listOf("Un límite."), speech.spoken)
        assertTrue(viewModel.now.speaking)

        viewModel.stopSpeaking()
        assertFalse(viewModel.now.speaking)

        viewModel.readAgain(earlier)
        assertTrue(viewModel.now.speaking)
    }

    @Test
    fun `a phone without synthesis says so and never shows speaking`() {
        speech.available = false
        client.historyResult = TutorResult.Success(listOf(earlier))
        val viewModel = viewModel()
        viewModel.readAgain(earlier)

        assertFalse(viewModel.now.speechAvailable)
        assertFalse(viewModel.now.speaking)
    }

    @Test
    fun `a spoken question is asked as soon as it is recognised`() {
        val viewModel = viewModel()
        viewModel.startVoice()
        assertTrue(viewModel.now.listening)
        assertEquals("es-ES", engine.lastLanguage)

        engine.listener.onPartial("qué es")
        assertEquals("qué es", viewModel.now.heard)
        engine.listener.onResult("qué es la derivada", 0.8)

        assertFalse(viewModel.now.listening)
        assertEquals("", viewModel.now.heard)
        assertEquals("qué es la derivada", client.asks.single().question)
    }

    @Test
    fun `asking by voice stops the reading in progress`() {
        client.historyResult = TutorResult.Success(listOf(earlier))
        val viewModel = viewModel()
        viewModel.readAgain(earlier)
        viewModel.startVoice()

        assertFalse(viewModel.now.speaking)
        assertTrue(speech.stops > 0)
    }

    @Test
    fun `a voice problem is shown and nothing is asked`() {
        val viewModel = viewModel()
        viewModel.startVoice()
        engine.listener.onError(RecognizerError.NO_SPEECH)

        assertEquals(VoiceProblem.NO_SPEECH, viewModel.now.voiceProblem)
        assertFalse(viewModel.now.listening)
        assertTrue(client.asks.isEmpty())

        viewModel.dismissVoiceProblem()
        assertNull(viewModel.now.voiceProblem)
        viewModel.onMicPermissionDenied()
        assertEquals(VoiceProblem.PERMISSION_DENIED, viewModel.now.voiceProblem)
    }

    @Test
    fun `a reached cost cap offers to continue, which asks again past the cap`() {
        val viewModel = viewModel()
        viewModel.onDraftChange("¿Qué?")
        viewModel.askDraft()
        val refused = TutorResult.Refused(409, "Se ha alcanzado el límite de gasto del día.", TutorResult.COST_CAP_REACHED)
        client.asks.single().reply.complete(refused)

        val failure = viewModel.now.failure!!
        assertEquals(AskFailure("¿Qué?", refused), failure)
        assertTrue(failure.overCap)
        assertNull(viewModel.now.pending)

        viewModel.confirmOverCap()

        assertEquals(2, client.asks.size)
        assertEquals("¿Qué?", client.asks[1].question)
        assertTrue(client.asks[1].confirmOverCap)
        assertNull(viewModel.now.failure)
    }

    @Test
    fun `any other failure is kept with its question and can be retried, not past the cap`() {
        val viewModel = viewModel()
        viewModel.onDraftChange("¿Qué?")
        viewModel.askDraft()
        client.asks.single().reply.complete(TutorResult.Interrupted)

        assertFalse(viewModel.now.failure!!.overCap)
        viewModel.confirmOverCap()
        assertEquals(1, client.asks.size)

        viewModel.retryFailed()
        assertEquals(2, client.asks.size)
        assertFalse(client.asks[1].confirmOverCap)

        client.asks[1].reply.complete(TutorResult.Unreachable("x"))
        viewModel.dismissFailure()
        assertNull(viewModel.now.failure)
    }

    @Test
    fun `nothing else is asked while a question is being answered`() {
        val viewModel = viewModel()
        viewModel.onDraftChange("¿Uno?")
        viewModel.askDraft()
        viewModel.onDraftChange("¿Dos?")
        viewModel.askDraft()
        viewModel.startVoice()

        assertEquals(1, client.asks.size)
        assertEquals("¿Dos?", viewModel.now.draft)
        assertEquals(0, engine.starts)
    }

    @Test
    fun `questions are capped at the backend's length`() {
        val viewModel = viewModel()
        viewModel.onDraftChange("x".repeat(MAX_QUESTION_CHARS + 50))
        assertEquals(MAX_QUESTION_CHARS, viewModel.now.draft.length)
    }

    @Test
    fun `going to the background stops listening and reading, releasing the recognizer`() {
        client.historyResult = TutorResult.Success(listOf(earlier))
        val viewModel = viewModel()
        viewModel.startVoice()
        val round = engine.listener
        viewModel.onBackground()
        round.onResult("tarde", null)

        assertFalse(viewModel.now.listening)
        assertFalse(viewModel.now.speaking)
        assertTrue(engine.destroyed)
        assertTrue(client.asks.isEmpty())
    }
}
