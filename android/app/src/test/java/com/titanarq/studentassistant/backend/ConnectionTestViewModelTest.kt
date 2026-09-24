package com.titanarq.studentassistant.backend

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.protocol.HealthResponse
import com.titanarq.studentassistant.protocol.HealthStatus
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.SubjectsListResponse
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
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

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val client = FakeBackendClient()
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val viewModel by lazy { ConnectionTestViewModel(client, store) }

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
        assertEquals(CheckState.Passed("2"), state.subjects)
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
        assertEquals(CheckState.Failed(BackendResult.HttpError(401)), state.subjects)
    }

    @Test
    fun `a failed health check still runs the authenticated call`() {
        runBlocking { store.save(home) }
        client.healthResult = BackendResult.InvalidResponse("not v1")
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(emptyList()))

        viewModel.run()

        val state = settled()
        assertEquals(CheckState.Failed(BackendResult.InvalidResponse("not v1")), state.health)
        assertEquals(CheckState.Passed("0"), state.subjects)
    }

    @Test
    fun `an unreachable backend fails both checks`() {
        runBlocking { store.save(home) }

        viewModel.run()

        val state = settled()
        assertEquals(CheckState.Failed(BackendResult.Unreachable("not scripted")), state.health)
        assertEquals(CheckState.Failed(BackendResult.Unreachable("not scripted")), state.subjects)
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
}
