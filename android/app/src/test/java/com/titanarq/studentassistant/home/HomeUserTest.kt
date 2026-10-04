package com.titanarq.studentassistant.home

import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.backend.PairedBackend
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionActiveStatus
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.SubjectsListResponse
import com.titanarq.studentassistant.protocol.Topic
import com.titanarq.studentassistant.protocol.TopicsListResponse
import com.titanarq.studentassistant.protocol.User
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.session.SessionHolder
import com.titanarq.studentassistant.users.UserHolder
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

/** The home screen acts for the selected user and signs them out (#554). */
class HomeUserTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val client = FakeBackendClient()
    private val sessions = SessionHolder()
    private val users = UserHolder()
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val viewModel by lazy { HomeViewModel(client, store, sessions, Clock { 1L }, users = users) }

    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "192.168.1.20:8000")
    private val laura = User("laura-mendez", "Laura Méndez")
    private val pablo = User("pablo", "Pablo Ruiz")
    private val historia = Subject("historia", "Historia")
    private val feudalismo = Topic("feudalismo", "historia", "El feudalismo")

    @After
    fun tearDown() {
        scope.cancel()
    }

    private fun until(predicate: (HomeUiState) -> Boolean): HomeUiState = runBlocking {
        withTimeout(5_000) { viewModel.state.first(predicate) }
    }

    @Test
    fun `every call is made as the selected user and the session carries them`() {
        runBlocking { store.save(home) }
        users.select(laura)
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(historia)))
        client.listTopicsResult = BackendResult.Success(TopicsListResponse("historia", listOf(feudalismo)))
        client.startSessionResult = BackendResult.Success(
            Session("s1", "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/s1", "1.8"),
        )

        viewModel.load()
        until { it.subjects is Loadable.Loaded }
        viewModel.selectSubject(historia)
        val row = (until { it.topics is Loadable.Loaded }.topics as Loadable.Loaded).value.single()
        viewModel.startOrContinue(row)
        until { it.openedSession != null }

        assertEquals(listOf("laura-mendez", "laura-mendez", "laura-mendez"), client.userIds)
        assertEquals("laura-mendez", sessions.current.value?.backend?.userId)
    }

    @Test
    fun `another user reloads the home as them`() {
        runBlocking { store.save(home) }
        users.select(laura)
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(historia)))
        viewModel.load()
        until { it.subjects is Loadable.Loaded }

        users.select(pablo)
        viewModel.load()
        until { it.subjects is Loadable.Loaded }

        assertEquals(listOf("laura-mendez", "pablo"), client.userIds)
    }

    @Test
    fun `sign-out clears the user and the session in the capture holder`() {
        runBlocking { store.save(home) }
        users.select(laura)
        sessions.open(
            OpenSession(
                home.credentials.forUser("laura-mendez"),
                Session("s1", "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/s1", "1.8"),
                "Historia",
                "El feudalismo",
            ),
        )
        assertEquals(laura, viewModel.user.value)

        viewModel.signOut()

        assertNull(viewModel.user.value)
        assertNull(users.current.value)
        assertNull(sessions.current.value)
    }
}
