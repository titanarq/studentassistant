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
import com.titanarq.studentassistant.session.PendingEnds
import com.titanarq.studentassistant.session.SessionHolder
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class HomeViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    // The store runs on the test thread: store work on Dispatchers.IO could outlive the test and
    // resume a view model on Dispatchers.Main after the rule reset it, failing a later test.
    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val client = FakeBackendClient()
    private val sessions = SessionHolder()
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val pendingEnds = FakePendingEnds()
    private val viewModel by lazy { HomeViewModel(client, store, sessions, Clock { NOW }, pendingEnds) }

    /** Pending ends the test sets; [continueInstead] records its calls and runs the resume. */
    private class FakePendingEnds : PendingEnds {
        override val pending = MutableStateFlow<Set<String>>(emptySet())
        val continued = mutableListOf<Pair<String, Boolean>>()

        override suspend fun continueInstead(sessionId: String, resume: suspend () -> Boolean): Boolean {
            val resumed = resume()
            continued += sessionId to resumed
            if (resumed) pending.value = pending.value - sessionId
            return resumed
        }
    }

    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "192.168.1.20:8000")
    private val historia = Subject("historia", "Historia")
    private val fisica = Subject("fisica", "Física")
    private val feudalismo = Topic("feudalismo", "historia", "El feudalismo")
    private val reconquista = Topic(
        "reconquista",
        "historia",
        "La Reconquista",
        openSessionId = "20260924-101500",
        lastSessionAtMs = 1_790_244_900_000,
        pendingCount = 2,
        digestExcerpt = "1 sesión con contenido; la última, el 24/09/2026: Covadonga. 2 dudas abiertas.",
    )

    @After
    fun tearDown() {
        scope.cancel()
    }

    private fun until(predicate: (HomeUiState) -> Boolean): HomeUiState = runBlocking {
        withTimeout(5_000) { viewModel.state.first(predicate) }
    }

    private fun session(id: String, topic: Topic) = Session(
        sessionId = id,
        subjectId = topic.subjectId,
        topicId = topic.topicId,
        status = SessionActiveStatus.ACTIVE,
        startedAtMs = 1_000,
        wsPath = "/ws/sessions/$id",
        protocolVersion = "1.0",
    )

    /** Stores the backend, scripts two subjects and the Historia topics, and opens Historia. */
    private fun showHistoria(): HomeUiState {
        runBlocking { store.save(home) }
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(historia, fisica)))
        client.listTopicsResult = BackendResult.Success(TopicsListResponse("historia", listOf(feudalismo, reconquista)))
        viewModel.load()
        until { it.subjects is Loadable.Loaded }
        viewModel.selectSubject(historia)
        return until { it.topics is Loadable.Loaded }
    }

    @Test
    fun `load lists the active backend's subjects`() {
        runBlocking { store.save(home) }
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(historia, fisica)))

        viewModel.load()

        val state = until { it.subjects is Loadable.Loaded }
        assertEquals("192.168.1.20:8000", state.backendName)
        assertEquals(Loadable.Loaded(listOf(historia, fisica)), state.subjects)
        assertNull(state.selectedSubject)
        assertEquals(listOf("listSubjects http://192.168.1.20:8000"), client.calls)
        assertEquals("sa_tok", client.lastToken)
    }

    @Test
    fun `without an active backend nothing is called`() {
        viewModel.load()

        assertTrue(until { it.noBackend }.noBackend)
        assertEquals(emptyList<String>(), client.calls)
    }

    @Test
    fun `a failed subject list is shown as a failure`() {
        runBlocking { store.save(home) }
        client.listSubjectsResult = BackendResult.HttpError(401)

        viewModel.load()

        assertEquals(Loadable.Failed(BackendResult.HttpError(401)), until { it.subjects is Loadable.Failed }.subjects)
    }

    @Test
    fun `selecting a subject lists its topics, an open session offers Continuar`() {
        val state = showHistoria()

        assertEquals(historia, state.selectedSubject)
        val rows = (state.topics as Loadable.Loaded).value
        assertEquals(listOf(feudalismo, reconquista), rows.map { it.topic })
        assertEquals(listOf(false, true), rows.map { it.canContinue })
        // The last session date, pending count and digest excerpt are mapped when sent, else null.
        assertEquals(listOf(null, 1_790_244_900_000), rows.map { it.lastSessionAtMs })
        assertEquals(listOf(null, 2), rows.map { it.pendingCount })
        assertEquals(listOf(null, reconquista.digestExcerpt), rows.map { it.digestExcerpt })
        assertEquals("listTopics http://192.168.1.20:8000 historia", client.calls.last())
    }

    @Test
    fun `clearing the subject goes back to the subject list`() {
        showHistoria()

        viewModel.clearSubject()

        val state = viewModel.state.value
        assertNull(state.selectedSubject)
        assertNull(state.topics)
    }

    @Test
    fun `reloading keeps the selected subject and refreshes its topics`() {
        showHistoria()
        client.listTopicsResult = BackendResult.Success(TopicsListResponse("historia", listOf(feudalismo)))

        viewModel.load()

        val state = until { (it.topics as? Loadable.Loaded)?.value?.size == 1 }
        assertEquals(historia, state.selectedSubject)
    }

    @Test
    fun `Empezar sesion starts a session and hands it to the capture screen`() {
        showHistoria()
        val started = session("20260924-120000", feudalismo)
        client.startSessionResult = BackendResult.Success(started)

        viewModel.startOrContinue(TopicRow(feudalismo))

        val state = until { it.openedSession != null }
        assertEquals(started, state.openedSession)
        assertEquals(SessionAction.Idle, state.session)
        assertEquals("startSession http://192.168.1.20:8000 feudalismo", client.calls.last())
        val open = sessions.current.value!!
        assertEquals(started, open.session)
        assertEquals("Historia", open.subjectName)
        assertEquals("El feudalismo", open.topicName)
        assertEquals("sa_tok", open.backend.token)

        viewModel.onSessionShown()
        assertNull(viewModel.state.value.openedSession)
    }

    @Test
    fun `Continuar resumes the topic's open session instead of starting one`() {
        showHistoria()
        val resumed = session("20260924-101500", reconquista)
        client.resumeSessionResult = BackendResult.Success(resumed)

        viewModel.startOrContinue(TopicRow(reconquista))

        assertEquals(resumed, until { it.openedSession != null }.openedSession)
        assertEquals("resumeSession http://192.168.1.20:8000 20260924-101500", client.calls.last())
        assertTrue(client.calls.none { it.startsWith("startSession") })
    }

    @Test
    fun `a 409 on start reports the conflict and refreshes the topics`() {
        showHistoria()
        client.startSessionResult = BackendResult.HttpError(409)
        val callsBefore = client.calls.size

        viewModel.startOrContinue(TopicRow(feudalismo))

        val state = until { it.session is SessionAction.Failed }
        assertEquals(SessionAction.Failed("feudalismo", SessionFailure.Conflict), state.session)
        until { client.calls.size >= callsBefore + 2 && it.topics is Loadable.Loaded }
        assertEquals("listTopics http://192.168.1.20:8000 historia", client.calls.last())
        assertNull(sessions.current.value)
        // The refresh keeps the failure on screen until the student dismisses it.
        assertTrue(viewModel.state.value.session is SessionAction.Failed)

        viewModel.dismissSessionFailure()
        assertEquals(SessionAction.Idle, viewModel.state.value.session)
    }

    @Test
    fun `an unreachable backend on start is reported as a backend failure`() {
        showHistoria()

        viewModel.startOrContinue(TopicRow(feudalismo))

        val state = until { it.session is SessionAction.Failed }
        assertEquals(
            SessionAction.Failed("feudalismo", SessionFailure.Backend(BackendResult.Unreachable("not scripted"))),
            state.session,
        )
        assertNull(state.openedSession)
    }

    @Test
    fun `creating a topic in an existing subject does not create the subject`() {
        showHistoria()
        viewModel.clearSubject()
        val created = Topic("cruzadas", "historia", "Las Cruzadas")
        client.createTopicResult = BackendResult.Success(created)
        client.listTopicsResult = BackendResult.Success(TopicsListResponse("historia", listOf(feudalismo, reconquista, created)))

        viewModel.openCreateTopic()
        viewModel.createTopic("  historia ", " Las Cruzadas ")

        val state = until { it.createTopic == null && (it.topics as? Loadable.Loaded)?.value?.size == 3 }
        assertEquals(historia, state.selectedSubject)
        assertTrue(client.calls.none { it.startsWith("createSubject") })
        assertTrue("createTopic http://192.168.1.20:8000 historia" in client.calls)
    }

    @Test
    fun `creating a topic in a new subject creates the subject first`() {
        runBlocking { store.save(home) }
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(historia)))
        viewModel.load()
        until { it.subjects is Loadable.Loaded }
        val quimica = Subject("quimica", "Química")
        val enlace = Topic("enlace", "quimica", "El enlace químico")
        client.createSubjectResult = BackendResult.Success(quimica)
        client.createTopicResult = BackendResult.Success(enlace)
        client.listTopicsResult = BackendResult.Success(TopicsListResponse("quimica", listOf(enlace)))

        viewModel.openCreateTopic()
        viewModel.createTopic("Química", "El enlace químico")

        val state = until { it.createTopic == null && it.topics is Loadable.Loaded }
        assertEquals(quimica, state.selectedSubject)
        assertEquals(Loadable.Loaded(listOf(historia, quimica)), state.subjects)
        assertEquals(
            listOf(
                "createSubject http://192.168.1.20:8000 Química",
                "createTopic http://192.168.1.20:8000 quimica",
                "listTopics http://192.168.1.20:8000 quimica",
            ),
            client.calls.drop(1),
        )
    }

    @Test
    fun `a failed topic creation keeps the dialog open with the failure`() {
        showHistoria()
        client.createTopicResult = BackendResult.HttpError(422)

        viewModel.openCreateTopic()
        viewModel.createTopic("Historia", "Las Cruzadas")

        val state = until { it.createTopic?.failure != null }
        assertEquals(CreateTopicDialog(saving = false, failure = BackendResult.HttpError(422)), state.createTopic)

        viewModel.dismissCreateTopic()
        assertNull(viewModel.state.value.createTopic)
    }

    @Test
    fun `a blank subject or title creates nothing`() {
        showHistoria()
        val callsBefore = client.calls.toList()

        viewModel.openCreateTopic()
        viewModel.createTopic("Historia", "   ")
        viewModel.createTopic(" ", "Las Cruzadas")

        assertEquals(callsBefore, client.calls)
        assertEquals(CreateTopicDialog(), viewModel.state.value.createTopic)
    }

    @Test
    fun `a topic whose session end is pending is marked, and the mark follows the finisher`() {
        pendingEnds.pending.value = setOf("20260924-101500")
        val rows = (showHistoria().topics as Loadable.Loaded).value
        assertEquals(listOf(false, true), rows.map { it.ending })

        // The end completes: the mark goes and the topics are fetched again.
        val listed = client.calls.count { it.startsWith("listTopics") }
        client.listTopicsResult = BackendResult.Success(
            TopicsListResponse("historia", listOf(feudalismo, reconquista.copy(openSessionId = null))),
        )
        pendingEnds.pending.value = emptySet()
        val state = until { state -> (state.topics as? Loadable.Loaded)?.value?.none { it.canContinue } == true }
        assertEquals(listOf(false, false), (state.topics as Loadable.Loaded).value.map { it.ending })
        assertEquals(listed + 1, client.calls.count { it.startsWith("listTopics") })
    }

    @Test
    fun `Continuar goes through the pending ends, so a pending end never races the resume`() {
        pendingEnds.pending.value = setOf("20260924-101500")
        showHistoria()
        val resumed = session("20260924-101500", reconquista)
        client.resumeSessionResult = BackendResult.Success(resumed)

        viewModel.startOrContinue(TopicRow(reconquista, ending = true))

        assertEquals(resumed, until { it.openedSession != null }.openedSession)
        assertEquals(listOf("20260924-101500" to true), pendingEnds.continued)

        client.resumeSessionResult = BackendResult.HttpError(409)
        viewModel.onSessionShown()
        viewModel.startOrContinue(TopicRow(reconquista))
        until { it.session is SessionAction.Failed }
        assertEquals("20260924-101500" to false, pendingEnds.continued.last())
    }

    private companion object {
        const val NOW = 1_758_700_000_000L
    }
}
