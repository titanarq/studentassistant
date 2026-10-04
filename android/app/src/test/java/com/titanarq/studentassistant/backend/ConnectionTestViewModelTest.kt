package com.titanarq.studentassistant.backend

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.protocol.ErrorCode
import com.titanarq.studentassistant.protocol.HealthResponse
import com.titanarq.studentassistant.protocol.HealthStatus
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.SubjectsListResponse
import com.titanarq.studentassistant.protocol.User
import com.titanarq.studentassistant.protocol.UsersListResponse
import com.titanarq.studentassistant.users.UserHolder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class ConnectionTestViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    // The store runs on the test thread: store work on Dispatchers.IO could outlive the test and
    // resume a view model on Dispatchers.Main after the rule reset it, failing a later test.
    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val client = FakeBackendClient()
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val laura = User("laura", "Laura")

    // A user is selected unless a test clears it (right after pairing nobody is).
    private val holder = UserHolder().apply { select(laura) }
    private val viewModel by lazy { ConnectionTestViewModel(client, store, holder) }

    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "192.168.1.20:8000")
    private val health = HealthResponse(HealthStatus.OK, "1.0", 5)

    @After
    fun tearDown() {
        scope.cancel()
    }

    private fun settled(): ConnectionTestUiState = runBlocking {
        viewModel.state.first { it.noBackend || (!it.running && it.health != CheckState.NotRun) }
    }

    @Test
    fun `health and subjects both pass on the active backend`() {
        runBlocking { store.save(home) }
        client.healthResult = BackendResult.Success(health)
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(Subject("s1", "Historia"), Subject("s2", "Física"))))

        viewModel.run()

        val state = settled()
        assertEquals("192.168.1.20:8000", state.backendName)
        assertEquals(CheckState.Passed("1.0"), state.health)
        assertEquals(CheckState.Passed("2"), state.access)
        assertEquals(listOf("health http://192.168.1.20:8000", "listSubjects http://192.168.1.20:8000"), client.calls)
        assertEquals("sa_tok", client.lastToken)
    }

    @Test
    fun `each check reports its own failure`() {
        runBlocking { store.save(home) }
        client.healthResult = BackendResult.Success(health)
        client.listSubjectsResult = BackendResult.HttpError(401)

        viewModel.run()

        val state = settled()
        assertEquals(CheckState.Passed("1.0"), state.health)
        assertEquals(CheckState.Failed(BackendResult.HttpError(401)), state.access)
    }

    @Test
    fun `a failed health check still runs the authenticated call`() {
        runBlocking { store.save(home) }
        client.healthResult = BackendResult.InvalidResponse("not v1")
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(emptyList()))

        viewModel.run()

        val state = settled()
        assertEquals(CheckState.Failed(BackendResult.InvalidResponse("not v1")), state.health)
        assertEquals(CheckState.Passed("0"), state.access)
    }

    @Test
    fun `an unreachable backend fails both checks`() {
        runBlocking { store.save(home) }

        viewModel.run()

        val state = settled()
        assertEquals(CheckState.Failed(BackendResult.Unreachable("not scripted")), state.health)
        assertEquals(CheckState.Failed(BackendResult.Unreachable("not scripted")), state.access)
        assertFalse(state.running)
    }

    @Test
    fun `without an active backend nothing is called`() {
        viewModel.run()

        assertEquals(ConnectionTestUiState(noBackend = true), settled())
        assertEquals(emptyList<String>(), client.calls)
    }

    @Test
    fun `the test runs on the backend that is active now`() {
        val lab = PairedBackend("http://10.0.0.5:8000", "d2", "sa_lab", "10.0.0.5:8000")
        runBlocking {
            store.save(home)
            store.save(lab)
            store.setActive("d1")
        }
        client.healthResult = BackendResult.Success(health)
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(emptyList()))

        viewModel.run()

        assertEquals("192.168.1.20:8000", settled().backendName)
        assertEquals("sa_tok", client.lastToken)
    }

    @Test
    fun `with a user selected the authorized check lists their subjects`() {
        runBlocking { store.save(home) }
        client.healthResult = BackendResult.Success(health)
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(Subject("s1", "Historia"))))

        viewModel.run()

        val state = settled()
        assertEquals(CheckState.Passed("1"), state.access)
        assertEquals(true, state.asUser)
        assertEquals(listOf("health http://192.168.1.20:8000", "listSubjects http://192.168.1.20:8000"), client.calls)
        assertEquals("laura", client.lastUserId)
    }

    @Test
    fun `with nobody selected and several users no user-scoped call is made`() {
        runBlocking { store.save(home) }
        holder.clear()
        client.healthResult = BackendResult.Success(health)
        client.listUsersResult = BackendResult.Success(UsersListResponse(listOf(laura, User("pablo", "Pablo"))))
        // A user-scoped call would be refused: the backend does not know who is asking.
        client.listSubjectsResult = BackendResult.HttpError(400, ErrorCode.USER_REQUIRED)

        viewModel.run()

        val state = settled()
        assertEquals(CheckState.Passed("1.0"), state.health)
        assertEquals(CheckState.Passed("2"), state.access)
        assertEquals(false, state.asUser)
        assertEquals(listOf("health http://192.168.1.20:8000", "listUsers http://192.168.1.20:8000"), client.calls)
        assertEquals("sa_tok", client.lastToken)
    }

    @Test
    fun `with nobody selected and exactly one user the test still succeeds`() {
        runBlocking { store.save(home) }
        holder.clear()
        client.healthResult = BackendResult.Success(health)
        client.listUsersResult = BackendResult.Success(UsersListResponse(listOf(laura)))

        viewModel.run()

        assertEquals(CheckState.Passed("1"), settled().access)
    }
}
