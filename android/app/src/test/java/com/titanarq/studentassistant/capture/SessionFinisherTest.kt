@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.AudioFormat
import com.titanarq.studentassistant.protocol.Button
import com.titanarq.studentassistant.protocol.ButtonName
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadResponse
import com.titanarq.studentassistant.protocol.CaptureUploadStatus
import com.titanarq.studentassistant.protocol.HelloAck
import com.titanarq.studentassistant.protocol.ServerAck
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionActiveStatus
import com.titanarq.studentassistant.protocol.SessionEndResponse
import com.titanarq.studentassistant.protocol.SessionEndedStatus
import com.titanarq.studentassistant.protocol.SttMode
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import com.titanarq.studentassistant.protocol.TranscriptFinal
import com.titanarq.studentassistant.spool.PendingEnd
import com.titanarq.studentassistant.spool.SpoolBudget
import com.titanarq.studentassistant.spool.SpooledFrame
import com.titanarq.studentassistant.spool.Spools
import java.io.File
import kotlinx.coroutines.ExperimentalCoroutinesApi
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

/** Disconnect / reconnect sequences around an end the backend could not take at once. */
class SessionFinisherTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val backend = BackendCredentials("http://pc:8000", "sa_tok")
    private val client = FakeBackendClient()
    private val sockets = FakeSessionSocketFactory()
    private val clock = FakeClock()
    private val end = PendingEnd("s1", backend.baseUrl, 9_000)
    private val root: File get() = File(folder.root, "spool")

    private fun session(received: List<String> = emptyList()) =
        Session("s1", "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/s1", "1.1", received)

    private val ended = BackendResult.Success(SessionEndResponse("s1", SessionEndedStatus.ENDED, 10_000))

    private fun spools() = Spools(root, SpoolBudget(10_000_000))

    private fun TestScope.uploads(spools: Spools, uploadClient: ScriptedUploadClient = ScriptedUploadClient(BackendResult.Unreachable("offline"))) =
        CaptureUploadQueue(backgroundScope, uploadClient, listOf(1_000), 10, spools.captures)

    private fun TestScope.finisher(spools: Spools, uploads: CaptureUploadQueue = uploads(spools)) = SessionFinisher(
        scope = backgroundScope,
        client = client,
        spools = spools,
        uploads = uploads,
        socketFactory = sockets,
        clock = clock,
        credentials = { if (it == backend.baseUrl) backend else null },
        retryDelaysMs = listOf(1_000),
        drainTimeoutMs = 30_000,
        capturesTimeoutMs = 60_000,
        reconnectDelaysMs = listOf(100),
    )

    private fun TestScope.handshake(mode: SttMode) {
        sockets.last.open()
        runCurrent()
        sockets.last.receive(HelloAck("1.1", mode, if (mode == SttMode.SERVER) AudioFormat() else null, 0, clock.now))
        runCurrent()
    }

    private fun endCalls() = client.calls.count { it.startsWith("endSession") }

    @Test
    fun `an end while offline waits for the backend, flushes the audio in order, then ends`() = runTest {
        val spools = spools()
        val audio = spools.audio("s1")
        (0L..3L).forEach { audio.append(SpooledFrame(it, 1_000 + it * 100, shortArrayOf(it.toShort()))) }
        client.resumeSessionResult = BackendResult.Unreachable("offline")
        client.endSessionResult = ended
        val finisher = finisher(spools)

        finisher.finish(backend, end)
        runCurrent()
        assertEquals(setOf("s1"), finisher.pending.value)
        assertEquals(listOf(end), spools.ends())
        assertEquals(listOf("resumeSession http://pc:8000 s1"), client.calls)
        assertTrue(sockets.sockets.isEmpty())

        client.resumeSessionResult = BackendResult.Success(session())
        advanceTimeBy(1_000)
        runCurrent()
        handshake(SttMode.SERVER)
        sockets.last.receive(ServerAck(audioSeq = 1, serverTimeMs = 1)) // the backend had 0..1
        runCurrent()
        assertEquals(listOf(2L, 3L), sockets.last.frames.map { it.seq })
        assertEquals(0, endCalls()) // not before the audio is acknowledged

        sockets.last.receive(ServerAck(audioSeq = 3, serverTimeMs = 2))
        runCurrent()
        assertTrue(sockets.last.closed)
        assertEquals("endSession http://pc:8000 s1", client.calls.last())
        assertEquals(emptySet<String>(), finisher.pending.value)
        assertEquals(emptyList<PendingEnd>(), spools.ends())
        assertEquals(emptyList<String>(), spools.sessionIds())
        assertEquals(0L, spools.budget.usedBytes)
    }

    @Test
    fun `a pending end left by a dead process is completed at the next start`() = runTest {
        val before = spools()
        before.putEnd(end)
        before.events("s1").putFinal(TranscriptClientFinal("a-3", 1, 2, "fin", "android-speech", "es-ES"))
        before.events("s1").enqueue(Button(ButtonName.END_SESSION, null, 9_000))

        // A new process.
        client.resumeSessionResult = BackendResult.Success(session())
        client.endSessionResult = ended
        val spools = spools()
        val finisher = finisher(spools)
        finisher.restore()
        runCurrent()
        handshake(SttMode.CLIENT)
        assertEquals(
            listOf(
                TranscriptClientFinal("a-3", 1, 2, "fin", "android-speech", "es-ES"),
                Button(ButtonName.END_SESSION, null, 9_000),
            ),
            sockets.last.sent.drop(1),
        )
        assertEquals(0, endCalls())
        sockets.last.receive(TranscriptFinal("a-3", 0, 1, "fin", "es-ES"))
        runCurrent()
        assertEquals(1, endCalls())
        assertEquals(emptyList<PendingEnd>(), spools.ends())
    }

    @Test
    fun `received captures are confirmed and the others upload before the end`() = runTest {
        val spools = spools()
        val uploadClient = ScriptedUploadClient(
            BackendResult.Unreachable("offline"),
            BackendResult.Unreachable("offline"),
            BackendResult.Success(CaptureUploadResponse("c-2", "s1", CaptureUploadStatus.STORED, 1, 1)),
        )
        val uploads = uploads(spools, uploadClient)
        val still = Still(CaptureImageBytes(byteArrayOf(1)), "image/jpeg", 10, 10, 1)
        listOf("c-1", "c-2").forEach { id ->
            uploads.begin(id, backend, "s1", CaptureTrigger.BUTTON, null, 1_000)
            uploads.submit(id, Burst(listOf(still), null))
        }
        runCurrent()
        client.resumeSessionResult = BackendResult.Success(session(received = listOf("c-1")))
        client.endSessionResult = ended
        val finisher = finisher(spools, uploads)

        finisher.finish(backend, end)
        runCurrent()
        assertEquals(ShotStatus.UPLOADED, uploads.all.value.first { it.captureId == "c-1" }.status)
        assertEquals(0, endCalls()) // c-2 still waits for its retry
        advanceTimeBy(1_000)
        runCurrent()
        assertEquals(ShotStatus.UPLOADED, uploads.all.value.first { it.captureId == "c-2" }.status)
        assertEquals(1, endCalls())
        assertEquals(1, uploadClient.uploads.count { it.metadata.captureId == "c-1" }) // never after the resume
        assertFalse(spools.captures.contains("c-1"))
        assertFalse(spools.captures.contains("c-2"))
    }

    @Test
    fun `a session already ended or gone completes the pending end`() = runTest {
        val spools = spools()
        spools.audio("s1").append(SpooledFrame(0, 1, shortArrayOf(1)))
        client.resumeSessionResult = BackendResult.HttpError(409)
        client.endSessionResult = BackendResult.HttpError(409)
        val finisher = finisher(spools)
        finisher.finish(backend, end)
        runCurrent()
        assertTrue(sockets.sockets.isEmpty()) // nothing can be flushed into an ended session
        assertEquals(1, endCalls())
        assertEquals(emptyList<PendingEnd>(), spools.ends())

        spools.putEnd(end.copy(sessionId = "s2"))
        client.resumeSessionResult = BackendResult.HttpError(404)
        finisher.finish(backend, end.copy(sessionId = "s2"))
        runCurrent()
        assertEquals(1, endCalls())
        assertEquals(emptyList<PendingEnd>(), spools.ends())
    }

    @Test
    fun `an end refused for good stays on disk for the next start, a transient one is retried`() = runTest {
        val spools = spools()
        client.resumeSessionResult = BackendResult.Success(session())
        client.endSessionResult = BackendResult.HttpError(503)
        val finisher = finisher(spools)
        finisher.finish(backend, end)
        runCurrent()
        assertEquals(1, endCalls())
        client.endSessionResult = BackendResult.HttpError(401)
        advanceTimeBy(1_000)
        runCurrent()
        assertEquals(2, endCalls())
        advanceTimeBy(10_000)
        runCurrent()
        assertEquals(2, endCalls())
        assertEquals(emptySet<String>(), finisher.pending.value)
        assertEquals(listOf(end), spools.ends())
    }
}
