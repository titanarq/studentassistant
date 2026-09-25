package com.titanarq.studentassistant.spool

import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.SubjectsListResponse
import com.titanarq.studentassistant.protocol.Topic
import com.titanarq.studentassistant.protocol.TopicsListResponse
import java.io.File
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Sweeping the spooled data of sessions over on the backend (#198). */
class StaleSpoolSweeperTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val backend = BackendCredentials("http://pc:8000", "sa_tok")
    private val client = FakeBackendClient()
    private val root: File get() = File(folder.root, "spool")
    private val hour = 60L * 60 * 1000

    /** The clock the sweep runs at: a day and an hour after the files were written. */
    private val later = Clock { System.currentTimeMillis() + 25 * hour }

    private fun spools() = Spools(root, SpoolBudget(10_000_000))

    private fun openOnBackend(vararg sessionIds: String) {
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(Subject("historia", "Historia"))))
        client.listTopicsResult = BackendResult.Success(
            TopicsListResponse(
                "historia",
                listOf(Topic("feudalismo", "historia", "El feudalismo")) +
                    sessionIds.mapIndexed { index, id -> Topic("t$index", "historia", "Tema $index", openSessionId = id) },
            ),
        )
    }

    private fun sweeper(
        spools: Spools,
        clock: Clock = later,
        paired: List<BackendCredentials> = listOf(backend),
        active: String? = null,
    ) = StaleSpoolSweeper(spools, client, clock, { paired }, { active }, graceMs = 24 * hour)

    /** A session with audio on disk, bound to [baseUrl] when given. */
    private fun Spools.session(id: String, baseUrl: String? = backend.baseUrl) {
        audio(id).append(SpooledFrame(0, 1, shortArrayOf(1)))
        audio(id).close()
        if (baseUrl != null) File(root, "sessions/$id/session.json").writeText("""{"base_url":"$baseUrl"}""")
    }

    private fun Spools.capture(id: String, sessionId: String) {
        val request = CaptureUploadRequest(id, CaptureTrigger.BUTTON, null, 1, listOf(CaptureImage("image_0", "image/jpeg", 1, 1, 1)))
        captures.put(SpooledCaptureMeta(sessionId, backend.baseUrl, request), listOf(CaptureImageBytes(byteArrayOf(1))), null)
    }

    @Test
    fun `after the grace period, data of sessions ended or unknown on the backend is deleted`() = runBlocking {
        spools().apply {
            session("s-ended")
            session("s-open")
            capture("c-ended", "s-ended")
            capture("c-open", "s-open")
        }
        openOnBackend("s-open")
        val spools = spools() // a new process
        val usedBefore = spools.budget.usedBytes

        val swept = sweeper(spools).sweep()

        assertEquals(SweepResult(listOf("s-ended"), listOf("c-ended")), swept)
        assertEquals(listOf("s-open"), spools.sessionIds())
        assertEquals(listOf("c-open"), spools.captures.list().map { it.captureId })
        assertTrue(spools.budget.usedBytes < usedBefore)
    }

    @Test
    fun `nothing is deleted within the grace period, and the backend is not even asked`() = runBlocking {
        val spools = spools()
        spools.session("s-ended")
        spools.capture("c-ended", "s-ended")
        openOnBackend()

        val swept = sweeper(spools, clock = Clock { System.currentTimeMillis() + 23 * hour }).sweep()

        assertEquals(SweepResult(), swept)
        assertEquals(listOf("s-ended"), spools.sessionIds())
        assertTrue(client.calls.isEmpty())
    }

    @Test
    fun `a backend that fails to answer, or is no longer paired, keeps its sessions`() = runBlocking {
        val spools = spools()
        spools.session("s1")
        spools.session("s2", baseUrl = "http://old-pc:8000")
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(Subject("historia", "Historia"))))
        client.listTopicsResult = BackendResult.HttpError(503)

        assertEquals(SweepResult(), sweeper(spools).sweep())
        openOnBackend()
        assertEquals(SweepResult(listOf("s1")), sweeper(spools).sweep()) // s2's backend cannot be asked
        assertEquals(listOf("s2"), spools.sessionIds())
    }

    @Test
    fun `a pending end, the session open in the app and a session bound here are left alone`() = runBlocking {
        spools().apply {
            session("s-ending")
            putEnd(PendingEnd("s-ending", backend.baseUrl, 1))
            session("s-active")
            session("s-bound")
        }
        openOnBackend()
        val spools = spools()
        spools.bind("s-bound", backend.baseUrl)

        assertEquals(SweepResult(), sweeper(spools, active = "s-active").sweep())
        assertEquals(listOf("s-active", "s-bound", "s-ending"), spools.sessionIds().sorted())
    }

    @Test
    fun `a session with no recorded backend goes only when every paired backend answers without it`() = runBlocking {
        val spools = spools()
        spools.session("s-legacy", baseUrl = null)
        openOnBackend("s-legacy")
        val other = BackendCredentials("http://laptop:8000", "sa_other")

        assertEquals(SweepResult(), sweeper(spools, paired = listOf(backend, other)).sweep())
        openOnBackend()
        assertEquals(SweepResult(listOf("s-legacy")), sweeper(spools, paired = listOf(backend, other)).sweep())
        assertFalse(File(root, "sessions/s-legacy").exists())
        assertEquals(emptyList<String>(), spools.sessionIds())
    }
}
