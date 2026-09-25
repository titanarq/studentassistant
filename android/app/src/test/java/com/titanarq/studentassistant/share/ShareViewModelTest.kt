package com.titanarq.studentassistant.share

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.backend.PairedBackend
import com.titanarq.studentassistant.home.Loadable
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.SubjectsListResponse
import com.titanarq.studentassistant.protocol.Topic
import com.titanarq.studentassistant.protocol.TopicsListResponse
import com.titanarq.studentassistant.protocol.WebPageAddRequest
import com.titanarq.studentassistant.protocol.WebPageAddResponse
import com.titanarq.studentassistant.protocol.WebPageVia
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
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class ShareViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val client = FakeBackendClient()
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }

    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "192.168.1.20:8000")
    private val historia = Subject("historia", "Historia")
    private val feudalismo = Topic("feudalismo", "historia", "El feudalismo")
    private val revolucion = Topic("revolucion", "historia", "La Revolución francesa", openSessionId = "20260925-101500")
    private val url = "https://historia.example.edu/bastilla"
    private val added = WebPageAddResponse(
        sourceId = "sources/web/001-la-bastilla.md",
        vaultId = "subjects/historia/topics/revolucion/sources/web/001-la-bastilla.md",
        title = "La Bastilla",
        url = url,
        alreadyKept = false,
    )

    @After
    fun tearDown() {
        scope.cancel()
    }

    private fun viewModel(text: String? = "La Bastilla\n$url") = ShareViewModel(text, null, client, store)

    private fun ShareViewModel.until(predicate: (ShareUiState) -> Boolean): ShareUiState = runBlocking {
        withTimeout(5_000) { state.first(predicate) }
    }

    /** Stores the backend, scripts Historia and its topics, and opens Historia. */
    private fun ShareViewModel.showHistoria(): ShareUiState {
        runBlocking { store.save(home) }
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(historia)))
        client.listTopicsResult = BackendResult.Success(TopicsListResponse("historia", listOf(feudalismo, revolucion)))
        load()
        until { it.subjects is Loadable.Loaded }
        selectSubject(historia)
        return until { it.topics is Loadable.Loaded }
    }

    @Test
    fun `the topics of the picked subject come with the open session first`() {
        val model = viewModel()
        val state = model.showHistoria()

        assertEquals(url, state.url)
        assertEquals("192.168.1.20:8000", state.backendName)
        assertEquals(Loadable.Loaded(listOf(revolucion, feudalismo)), state.topics)
        assertEquals(listOf("listSubjects http://192.168.1.20:8000", "listTopics http://192.168.1.20:8000 historia"), client.calls)
    }

    @Test
    fun `picking a topic saves the link there as a share`() {
        val model = viewModel()
        model.showHistoria()
        client.addWebPageResult = BackendResult.Success(added)

        model.save(revolucion)

        val state = model.until { it.outcome != null }
        assertEquals(ShareOutcome.Saved(revolucion, "La Bastilla", alreadyKept = false), state.outcome)
        assertNull(state.saving)
        assertEquals(WebPageAddRequest(url, WebPageVia.SHARE), client.lastWebPageRequest)
        assertEquals("addWebPage http://192.168.1.20:8000 historia/revolucion", client.calls.last())
        assertEquals("sa_tok", client.lastToken)
        // Once saved, another pick does nothing.
        model.save(feudalismo)
        assertEquals(1, client.calls.count { it.startsWith("addWebPage") })
    }

    @Test
    fun `a page the topic already had is reported as such`() {
        val model = viewModel()
        model.showHistoria()
        client.addWebPageResult = BackendResult.Success(added.copy(alreadyKept = true))

        model.save(feudalismo)

        assertEquals(ShareOutcome.Saved(feudalismo, "La Bastilla", alreadyKept = true), model.until { it.outcome != null }.outcome)
    }

    @Test
    fun `a refusal is shown and another topic can be picked`() {
        val model = viewModel()
        model.showHistoria()
        client.addWebPageResult = BackendResult.HttpError(422)

        model.save(feudalismo)
        assertEquals(ShareOutcome.Failed(feudalismo, BackendResult.HttpError(422)), model.until { it.outcome != null }.outcome)

        client.addWebPageResult = BackendResult.Success(added)
        model.save(revolucion)
        assertTrue(model.until { it.outcome is ShareOutcome.Saved }.outcome is ShareOutcome.Saved)
    }

    @Test
    fun `a share without a link calls nothing`() {
        runBlocking { store.save(home) }
        val model = viewModel("unos apuntes sin enlace")

        model.load()

        assertNull(model.state.value.url)
        assertEquals(emptyList<String>(), client.calls)
    }

    @Test
    fun `without a paired backend it says so`() {
        val model = viewModel()

        model.load()

        assertTrue(model.until { it.noBackend }.noBackend)
        assertEquals(emptyList<String>(), client.calls)
    }
}
