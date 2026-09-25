package com.titanarq.studentassistant.desk

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.PairedBackend
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
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class StudyDeskViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    // The store runs on the test thread (see ConnectionTestViewModelTest).
    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val topic = DeskTopic("historia", "revolucion", "La Revolución francesa")

    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "Portátil")

    @After
    fun tearDown() {
        scope.cancel()
    }

    private fun settled(viewModel: StudyDeskViewModel): DeskUiState = runBlocking {
        withTimeout(5_000) { viewModel.state.first { it != DeskUiState.Loading } }
    }

    private fun ready(): Pair<StudyDeskViewModel, DeskUiState.Ready> {
        runBlocking { store.save(home) }
        val viewModel = StudyDeskViewModel(store, topic)
        return viewModel to settled(viewModel) as DeskUiState.Ready
    }

    @Test
    fun `the active backend's notes page is loaded with its token cookie`() {
        val (_, state) = ready()

        assertEquals("Portátil", state.backendName)
        assertEquals("http://192.168.1.20:8000", state.baseUrl)
        assertEquals("http://192.168.1.20:8000/subjects/historia/topics/revolucion/notes", state.page.url)
        assertEquals("http://192.168.1.20:8000/", state.page.cookieUrl)
        assertEquals("sa_token=sa_tok; Path=/; HttpOnly; SameSite=Strict", state.page.cookie)
        assertEquals(0, state.reload)
        assertNull(state.failure)
    }

    @Test
    fun `without an active backend nothing is loaded`() {
        assertEquals(DeskUiState.NoBackend, settled(StudyDeskViewModel(store, topic)))
    }

    @Test
    fun `a stored address that is not http is reported`() {
        runBlocking { store.save(home.copy(baseUrl = "192.168.1.20:8000")) }

        assertEquals(DeskUiState.InvalidBackend, settled(StudyDeskViewModel(store, topic)))
    }

    @Test
    fun `a 401 asks to pair again, other failures keep their detail`() {
        val (viewModel, _) = ready()

        viewModel.onLoadFailed(401, "Unauthorized")
        assertEquals(DeskLoadFailure.Unauthorized, (viewModel.state.value as DeskUiState.Ready).failure)

        viewModel.onLoadFailed(503, "Service Unavailable")
        assertEquals(DeskLoadFailure.Failed("HTTP 503"), (viewModel.state.value as DeskUiState.Ready).failure)

        viewModel.onLoadFailed(null, "net::ERR_CONNECTION_REFUSED")
        assertEquals(
            DeskLoadFailure.Failed("net::ERR_CONNECTION_REFUSED"),
            (viewModel.state.value as DeskUiState.Ready).failure,
        )
    }

    @Test
    fun `retry clears the failure and asks for a new load`() {
        val (viewModel, _) = ready()
        viewModel.onLoadFailed(null, "net::ERR_CONNECTION_REFUSED")

        viewModel.retry()
        viewModel.retry()

        val state = viewModel.state.value as DeskUiState.Ready
        assertNull(state.failure)
        assertEquals(2, state.reload)
    }
}
