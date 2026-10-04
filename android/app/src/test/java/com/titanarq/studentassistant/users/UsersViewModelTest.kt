package com.titanarq.studentassistant.users

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.backend.PairedBackend
import com.titanarq.studentassistant.protocol.User
import com.titanarq.studentassistant.protocol.UsersListResponse
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class UsersViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val client = FakeBackendClient()
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val holder = UserHolder()
    private val viewModel by lazy { UsersViewModel(client, store, holder) }

    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "192.168.1.20:8000")
    private val laura = User("laura-mendez", "Laura Méndez", email = "laura@example.com")
    private val pablo = User("pablo", "Pablo Ruiz", photoUrl = "/api/users/pablo/photo")

    @After
    fun tearDown() {
        scope.cancel()
    }

    @Test
    fun `the active backend's users are listed, without a user header`() {
        runBlocking { store.save(home) }
        client.listUsersResult = BackendResult.Success(UsersListResponse(listOf(laura, pablo)))

        viewModel.load()

        assertEquals(UsersUiState.Loaded(listOf(laura, pablo)), viewModel.state.value)
        assertEquals(listOf("listUsers http://192.168.1.20:8000"), client.calls)
        assertEquals("sa_tok", client.lastToken)
        assertNull(client.lastUserId)
    }

    @Test
    fun `a vault without users is an empty list`() {
        runBlocking { store.save(home) }
        client.listUsersResult = BackendResult.Success(UsersListResponse(emptyList()))

        viewModel.load()

        assertEquals(UsersUiState.Loaded(emptyList()), viewModel.state.value)
    }

    @Test
    fun `a failure is shown and Reintentar loads again`() {
        runBlocking { store.save(home) }
        client.listUsersResult = BackendResult.Unreachable("timeout")
        viewModel.load()
        assertEquals(UsersUiState.Failed(BackendResult.Unreachable("timeout")), viewModel.state.value)

        client.listUsersResult = BackendResult.Success(UsersListResponse(listOf(laura)))
        viewModel.load()

        assertEquals(UsersUiState.Loaded(listOf(laura)), viewModel.state.value)
        assertEquals(2, client.calls.size)
    }

    @Test
    fun `without a stored backend nothing is asked`() {
        viewModel.load()

        assertEquals(UsersUiState.NoBackend, viewModel.state.value)
        assertEquals(emptyList<String>(), client.calls)
    }

    @Test
    fun `choosing a user keeps them in memory`() {
        assertNull(holder.current.value)

        viewModel.select(pablo)

        assertEquals(pablo, holder.current.value)
    }

    @Test
    fun `initials stand for a name without photo`() {
        assertEquals("LM", initialsOf("Laura Méndez"))
        assertEquals("P", initialsOf("pablo"))
        assertEquals("AG", initialsOf("  ana  maría  gómez "))
        assertEquals("?", initialsOf("  "))
    }
}
