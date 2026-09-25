package com.titanarq.studentassistant

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.MutableCreationExtras
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.ConnectionTestViewModel
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.backend.OkHttpBackendClient
import com.titanarq.studentassistant.backend.PairedBackendsViewModel
import com.titanarq.studentassistant.capture.CaptureViewModel
import com.titanarq.studentassistant.capture.FakeSessionSocketFactory
import com.titanarq.studentassistant.capture.NoStillCamera
import com.titanarq.studentassistant.home.HomeViewModel
import com.titanarq.studentassistant.pairing.PairingViewModel
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionActiveStatus
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.capture.FakeRecognizerEngine
import com.titanarq.studentassistant.tutor.FakeSpeechOutput
import com.titanarq.studentassistant.tutor.FakeTutorClient
import com.titanarq.studentassistant.tutor.NoSpeechOutput
import com.titanarq.studentassistant.tutor.OkHttpTutorClient
import com.titanarq.studentassistant.tutor.TutorTopic
import com.titanarq.studentassistant.tutor.TutorViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.cancel
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class AppContainerTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    // The store runs on the test thread: store work on Dispatchers.IO could outlive the test and
    // resume a view model on Dispatchers.Main after the rule reset it, failing a later test.
    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())

    @After
    fun tearDown() {
        scope.cancel()
    }

    private fun <T : ViewModel> ViewModelProvider.Factory.make(type: Class<T>): T = create(type, MutableCreationExtras())

    @Test
    fun `a container can be constructed with its defaults`() {
        val container = AppContainer(filesDir = folder.root)

        assertSame(SystemClock, container.clock)
        assertTrue(container.backendClient is OkHttpBackendClient)
        assertEquals("Android", container.deviceName)
        assertTrue(container.tutorClient is OkHttpTutorClient)
        assertSame(NoSpeechOutput, container.speechOutput)
    }

    @Test
    fun `the tutor view-model factory builds one view model per topic over the app's one voice`() {
        val speech = FakeSpeechOutput()
        val engines = mutableListOf<FakeRecognizerEngine>()
        val container = AppContainer(
            filesDir = folder.root,
            backendStoreFactory = { dir -> BackendStore.create(File(dir, BackendStore.FILE_NAME), scope) },
            tutorClientFactory = { FakeTutorClient() },
            recognizerEngineFactory = { FakeRecognizerEngine().also { engines += it } },
            speechOutputFactory = { speech },
        )
        val topic = TutorTopic("calculo", "derivadas", "Derivadas")

        val first = container.tutorViewModelFactory(topic).make(TutorViewModel::class.java)
        val second = container.tutorViewModelFactory(topic).make(TutorViewModel::class.java)

        assertEquals(topic, first.topic)
        assertTrue(first !== second)
        assertEquals(2, engines.size)
        assertSame(speech, container.speechOutput)
    }

    @Test
    fun `a member is created lazily once and the same instance is returned on repeated access`() {
        var created = 0
        val container = AppContainer(filesDir = folder.root, clockFactory = {
            created++
            Clock { 42L }
        })
        assertEquals(0, created)

        val first = container.clock
        val second = container.clock

        assertSame(first, second)
        assertEquals(1, created)
        assertEquals(42L, first.nowMillis())
    }

    @Test
    fun `the backend store lives in the files dir and is a single instance`() {
        var dirSeen: File? = null
        val container = AppContainer(
            filesDir = folder.root,
            backendStoreFactory = { dir ->
                dirSeen = dir
                BackendStore.create(File(dir, BackendStore.FILE_NAME), scope)
            },
        )

        assertSame(container.backendStore, container.backendStore)
        assertEquals(folder.root, dirSeen)
    }

    @Test
    fun `the view-model factories build the pairing, backends, connection-test and home view models`() {
        val fake = FakeBackendClient()
        val container = AppContainer(
            filesDir = folder.root,
            deviceName = "Pixel 8",
            backendClientFactory = { fake },
            backendStoreFactory = { dir -> BackendStore.create(File(dir, BackendStore.FILE_NAME), scope) },
        )

        assertSame(fake, container.backendClient)
        assertTrue(container.pairingViewModelFactory.make(PairingViewModel::class.java) is PairingViewModel)
        assertTrue(container.pairedBackendsViewModelFactory.make(PairedBackendsViewModel::class.java) is PairedBackendsViewModel)
        assertTrue(container.connectionTestViewModelFactory.make(ConnectionTestViewModel::class.java) is ConnectionTestViewModel)
        assertTrue(container.homeViewModelFactory.make(HomeViewModel::class.java) is HomeViewModel)
        assertSame(container.sessionHolder, container.sessionHolder)
    }

    @Test
    fun `the capture view-model factory builds a view model for the given session`() {
        val sockets = FakeSessionSocketFactory()
        val container = AppContainer(
            filesDir = folder.root,
            backendClientFactory = { FakeBackendClient() },
            sessionSocketFactoryFactory = { sockets },
            backendStoreFactory = { dir -> BackendStore.create(File(dir, BackendStore.FILE_NAME), scope) },
            uploadScope = scope,
        )
        val open = OpenSession(
            backend = BackendCredentials("http://pc:8000", "sa_tok"),
            session = Session("s1", "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/s1", "1.1"),
            subjectName = "Historia",
            topicName = "El feudalismo",
        )
        val viewModel = container.captureViewModelFactory(open).make(CaptureViewModel::class.java)
        assertEquals("El feudalismo", viewModel.state.value.topicName)
        assertSame(sockets, container.sessionSocketFactory)
        assertSame(NoStillCamera, container.stillCamera)
    }
}
