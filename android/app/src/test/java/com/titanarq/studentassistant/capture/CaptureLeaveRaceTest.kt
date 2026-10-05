@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.HelloAck
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionActiveStatus
import com.titanarq.studentassistant.protocol.SessionEndResponse
import com.titanarq.studentassistant.protocol.SessionEndedStatus
import com.titanarq.studentassistant.protocol.SttMode
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.session.SessionHolder
import com.titanarq.studentassistant.spool.SpoolBudget
import com.titanarq.studentassistant.spool.Spools
import java.io.File
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * #583: leaving the capture screen must end the session even though the screen's view model scope
 * is cancelled right away (the screen is gone), and a new session must wait for that end.
 */
class CaptureLeaveRaceTest {
    @get:Rule
    val main = MainDispatcherRule(StandardTestDispatcher())

    @get:Rule
    val folder = TemporaryFolder()

    private val clock = FakeClock(2_000_000)
    private val backend = FakeBackendClient()
    private val sockets = FakeSessionSocketFactory()
    private val credentials = BackendCredentials("http://192.168.1.20:8000", "sa_tok")
    private val ended = BackendResult.Success(SessionEndResponse("s1", SessionEndedStatus.ENDED, 9))
    private fun session(id: String) =
        Session(id, "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/$id", "1.1")

    private data class Entered(val viewModel: CaptureViewModel, val finisher: SessionFinisher)

    private fun TestScope.enter(spools: Spools, id: String): Entered {
        val uploads = CaptureUploadQueue(backgroundScope, backend, spool = spools.captures)
        val finisher = finisherFor(spools, uploads)
        return enter(spools, id, finisher)
    }

    private fun TestScope.finisherFor(spools: Spools, uploads: CaptureUploadQueue) = SessionFinisher(
        scope = backgroundScope,
        client = backend,
        spools = spools,
        uploads = uploads,
        socketFactory = sockets,
        clock = clock,
        credentials = { credentials },
        retryDelaysMs = listOf(1_000),
        reconnectDelaysMs = listOf(100),
    )

    private fun TestScope.enter(spools: Spools, id: String, finisher: SessionFinisher): Entered {
        val open = OpenSession(credentials, session(id), "Historia", "El feudalismo").also(SessionHolder()::open)
        val viewModel = CaptureViewModel(
            open = open,
            backendClient = backend,
            sessionHolder = SessionHolder(),
            clock = clock,
            socketFactory = sockets,
            transcriberFactory = { FakeTranscriber() },
            audioStreamerFactory = { error("server mode not used") },
            stillCapture = FakeStillCapture(),
            reconnectDelaysMs = listOf(100),
            spooling = CaptureSpooling(spools.audio(id), spools.events(id), spools.budget.nearCap, finisher),
        )
        viewModel.start()
        runCurrent()
        sockets.last.open()
        runCurrent()
        sockets.last.receive(HelloAck("1.1", SttMode.CLIENT, null, 0, clock.now))
        runCurrent()
        return Entered(viewModel, finisher)
    }

    @Test
    fun `the end reaches the backend although the screen's scope is cancelled at once`() = runTest(main.dispatcher) {
        val spools = Spools(File(folder.root, "spool"), SpoolBudget(10_000_000))
        backend.resumeSessionResult = BackendResult.Success(session("s1"))
        backend.endSessionResult = ended
        val (viewModel, finisher) = enter(spools, "s1")

        viewModel.endOnLeave()
        viewModel.viewModelScope.cancel() // the screen is left: the view model is cleared
        runCurrent()
        advanceTimeBy(10_000)
        runCurrent()

        assertEquals(1, backend.calls.count { it.startsWith("endSession") })
        assertEquals(emptySet<String>(), finisher.pending.value)
        assertTrue(spools.ends().isEmpty())
    }

    @Test
    fun `enter, leave and enter again quickly waits for the end instead of finding the old session open`() =
        runTest(main.dispatcher) {
            val spools = Spools(File(folder.root, "spool"), SpoolBudget(10_000_000))
            backend.resumeSessionResult = BackendResult.Success(session("s1"))
            // The backend only takes the end at the second try: the first answer is a transient failure.
            backend.endSessionResult = BackendResult.Unreachable("timeout")
            val (first, finisher) = enter(spools, "s1")

            first.endOnLeave()
            first.viewModelScope.cancel()
            runCurrent()
            // Back on the topic list the student taps the topic again: the end is still pending.
            assertEquals(setOf("s1"), finisher.pending.value)
            assertFalse(finisher.awaitIdle(1))

            backend.endSessionResult = ended
            advanceTimeBy(1_000)
            runCurrent()
            assertTrue(finisher.awaitIdle(1))
            assertEquals(emptySet<String>(), finisher.pending.value)
            assertTrue(backend.calls.last().startsWith("endSession"))
        }

    @Test
    fun `a view model dropped while running still ends its session`() = runTest(main.dispatcher) {
        val spools = Spools(File(folder.root, "spool"), SpoolBudget(10_000_000))
        backend.resumeSessionResult = BackendResult.Success(session("s1"))
        backend.endSessionResult = ended
        val (viewModel, finisher) = enter(spools, "s1")

        val onCleared = CaptureViewModel::class.java.getDeclaredMethod("onCleared")
        onCleared.isAccessible = true
        onCleared.invoke(viewModel)
        runCurrent()
        advanceTimeBy(10_000)
        runCurrent()

        assertEquals(1, backend.calls.count { it.startsWith("endSession") })
        assertEquals(emptySet<String>(), finisher.pending.value)
    }
}
