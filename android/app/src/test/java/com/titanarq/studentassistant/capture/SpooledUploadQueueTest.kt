@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadResponse
import com.titanarq.studentassistant.protocol.CaptureUploadStatus
import com.titanarq.studentassistant.spool.CaptureSpool
import com.titanarq.studentassistant.spool.SpoolBudget
import java.io.File
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** [CaptureUploadQueue] over a [CaptureSpool]: what survives a restart and what is never resent. */
class SpooledUploadQueueTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val backend = BackendCredentials("http://pc:8000", "sa_tok")
    private val stills = listOf(
        Still(CaptureImageBytes(byteArrayOf(1, 1)), "image/jpeg", 3000, 4000, 1_010),
        Still(CaptureImageBytes(byteArrayOf(2, 2)), "image/jpeg", 3000, 4000, 1_020),
    )
    private val thumbnail = CaptureImageBytes(byteArrayOf(9))
    private val dir: File get() = File(folder.root, "captures")

    private fun stored(id: String, status: CaptureUploadStatus = CaptureUploadStatus.STORED) =
        BackendResult.Success(CaptureUploadResponse(id, "s1", status, 2, 5_000))

    private fun spool() = CaptureSpool(dir, SpoolBudget(1_000_000))

    private fun TestScope.queue(client: ScriptedUploadClient, spool: CaptureSpool = spool()) =
        CaptureUploadQueue(backgroundScope, client, listOf(1_000), 10, spool)

    private fun CaptureUploadQueue.take(id: String, time: Long = 1_000) {
        begin(id, backend, "s1", CaptureTrigger.BUTTON, null, time)
        submit(id, Burst(stills, thumbnail))
    }

    private fun CaptureUploadQueue.status(id: String) = all.value.single { it.captureId == id }.status

    @Test
    fun `a burst is on disk until the backend has it`() = runTest {
        val client = ScriptedUploadClient(BackendResult.Unreachable("offline"), stored("c-1"))
        val spool = spool()
        val queue = queue(client, spool)
        queue.take("c-1")
        runCurrent()
        assertEquals(ShotStatus.PENDING, queue.status("c-1"))
        assertTrue(spool.contains("c-1"))

        advanceTimeBy(1_000)
        runCurrent()
        assertEquals(ShotStatus.UPLOADED, queue.status("c-1"))
        assertFalse(spool.contains("c-1"))
        // The stills went out read back from disk, byte for byte.
        assertArrayEquals(byteArrayOf(2, 2), client.uploads.last().images[1].bytes)
    }

    @Test
    fun `after a restart the spooled captures upload again, oldest first, with the same ids`() = runTest {
        val offline = ScriptedUploadClient(BackendResult.Unreachable("offline"))
        val first = queue(offline)
        first.take("c-2", time = 2_000)
        first.take("c-1", time = 1_000)
        runCurrent()

        // A new process: a new queue over the same directory.
        val client = ScriptedUploadClient(stored("c-1"), stored("c-2", CaptureUploadStatus.DUPLICATE))
        val spool = spool()
        val restarted = queue(client, spool)
        restarted.restore { baseUrl -> if (baseUrl == backend.baseUrl) backend else null }
        runCurrent()

        assertEquals(listOf("c-1", "c-2"), client.uploads.map { it.metadata.captureId })
        assertEquals(backend, client.uploads.first().backend)
        assertEquals(1_000L, client.uploads.first().metadata.clientTimeMs)
        assertEquals(listOf(ShotStatus.UPLOADED, ShotStatus.UPLOADED), restarted.all.value.map { it.status })
        assertArrayEquals(byteArrayOf(9), restarted.all.value.first().thumbnail!!.bytes)
        assertFalse(spool.contains("c-1"))
        assertFalse(spool.contains("c-2")) // `duplicate` counts as uploaded
    }

    @Test
    fun `a capture of a backend no longer paired stays on disk`() = runTest {
        val first = queue(ScriptedUploadClient(BackendResult.Unreachable("offline")))
        first.take("c-1")
        runCurrent()

        val client = ScriptedUploadClient(stored("c-1"))
        val spool = spool()
        val restarted = queue(client, spool)
        restarted.restore { null }
        runCurrent()
        assertTrue(client.uploads.isEmpty())
        assertTrue(spool.contains("c-1"))
    }

    @Test
    fun `captures the resume lists as received are never sent again`() = runTest {
        val first = queue(ScriptedUploadClient(BackendResult.Unreachable("offline")))
        first.take("c-1", time = 1_000)
        first.take("c-2", time = 2_000)
        runCurrent()

        // The backend stored c-1 but its answer was lost: the resume lists it.
        val client = ScriptedUploadClient(BackendResult.Unreachable("offline"), stored("c-2"))
        val spool = spool()
        val restarted = queue(client, spool)
        restarted.restore { backend }
        runCurrent()
        restarted.confirmReceived("s1", listOf("c-1"))
        assertEquals(ShotStatus.UPLOADED, restarted.status("c-1"))
        assertFalse(spool.contains("c-1"))
        advanceTimeBy(1_000)
        runCurrent()
        assertEquals(1, client.uploads.count { it.metadata.captureId == "c-1" }) // only the first try, before the resume
        assertEquals(ShotStatus.UPLOADED, restarted.status("c-2"))
        assertFalse(restarted.hasPending("s1"))
    }

    @Test
    fun `a capture refused for good stays spooled until its session is forgotten`() = runTest {
        val client = ScriptedUploadClient(BackendResult.HttpError(422), stored("c-1"))
        val spool = spool()
        val queue = queue(client, spool)
        queue.take("c-1")
        runCurrent()
        assertEquals(ShotStatus.FAILED, queue.status("c-1"))
        assertTrue(spool.contains("c-1"))

        queue.retryFailed("s1")
        runCurrent()
        assertEquals(ShotStatus.UPLOADED, queue.status("c-1"))

        val refused = queue(ScriptedUploadClient(BackendResult.HttpError(422)), spool)
        refused.take("c-9")
        runCurrent()
        refused.forgetSession("s1")
        assertFalse(spool.contains("c-9"))
    }
}
