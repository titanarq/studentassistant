@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.CaptureUploadResponse
import com.titanarq.studentassistant.protocol.CaptureUploadStatus
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

/** A [BackendClient] whose uploads answer from a script (the last answer repeats) and are recorded. */
class ScriptedUploadClient(
    vararg answers: BackendResult<CaptureUploadResponse>,
) : BackendClient by FakeBackendClient() {
    private val script = answers.toMutableList()

    /** Set to hold every upload until completed. */
    var gate: CompletableDeferred<Unit>? = null

    data class Upload(val backend: BackendCredentials, val sessionId: String, val metadata: CaptureUploadRequest, val images: List<CaptureImageBytes>)

    val uploads = mutableListOf<Upload>()

    override suspend fun uploadCapture(
        backend: BackendCredentials,
        sessionId: String,
        metadata: CaptureUploadRequest,
        images: List<CaptureImageBytes>,
    ): BackendResult<CaptureUploadResponse> {
        uploads += Upload(backend, sessionId, metadata, images)
        gate?.await()
        return if (script.size > 1) script.removeAt(0) else script.single()
    }
}

class CaptureUploadQueueTest {
    private val backend = BackendCredentials("http://pc:8000", "sa_tok")
    private val stills = listOf(
        Still(CaptureImageBytes(byteArrayOf(1)), "image/jpeg", 3000, 4000, 1_010),
        Still(CaptureImageBytes(byteArrayOf(2)), "image/jpeg", 3000, 4000, 1_020),
        Still(CaptureImageBytes(byteArrayOf(3)), "image/jpeg", 3000, 4000, 1_030),
    )
    private val thumbnail = CaptureImageBytes(byteArrayOf(9))

    private fun stored(id: String = "c-1", status: CaptureUploadStatus = CaptureUploadStatus.STORED) =
        BackendResult.Success(CaptureUploadResponse(id, "s1", status, 3, 5_000))

    private fun TestScope.queue(client: BackendClient, delays: List<Long> = listOf(1_000, 2_000), conflicts: Int = 10) =
        CaptureUploadQueue(backgroundScope, client, delays, conflicts)

    private fun CaptureUploadQueue.shot(id: String = "c-1") = all.value.single { it.captureId == id }

    private fun CaptureUploadQueue.take(id: String = "c-1", trigger: CaptureTrigger = CaptureTrigger.BUTTON, commandId: String? = null) {
        begin(id, backend, "s1", trigger, commandId, 1_000)
        submit(id, Burst(stills, thumbnail))
    }

    @Test
    fun `a burst uploads as one multipart request built from its stills`() = runTest {
        val client = ScriptedUploadClient(stored())
        val queue = queue(client)
        queue.begin("c-1", backend, "s1", CaptureTrigger.COMMAND, "cmd-7", 1_000)
        assertEquals(ShotStatus.CAPTURING, queue.shot().status)
        assertNull(queue.shot().thumbnail)

        queue.submit("c-1", Burst(stills, thumbnail))
        assertEquals(ShotStatus.PENDING, queue.shot().status)
        assertSame(thumbnail, queue.shot().thumbnail)
        runCurrent()

        val upload = client.uploads.single()
        assertEquals(backend, upload.backend)
        assertEquals("s1", upload.sessionId)
        assertEquals(
            CaptureUploadRequest(
                captureId = "c-1",
                trigger = CaptureTrigger.COMMAND,
                commandId = "cmd-7",
                clientTimeMs = 1_000,
                images = listOf(
                    CaptureImage("image_0", "image/jpeg", 3000, 4000, 1_010),
                    CaptureImage("image_1", "image/jpeg", 3000, 4000, 1_020),
                    CaptureImage("image_2", "image/jpeg", 3000, 4000, 1_030),
                ),
            ),
            upload.metadata,
        )
        assertEquals(stills.map { it.bytes }, upload.images)
        assertEquals(ShotStatus.UPLOADED, queue.shot().status)
    }

    @Test
    fun `the strip shows uploading while the request is in flight`() = runTest {
        val client = ScriptedUploadClient(stored()).apply { gate = CompletableDeferred() }
        val queue = queue(client)
        queue.take()
        runCurrent()
        assertEquals(ShotStatus.UPLOADING, queue.shot().status)
        client.gate!!.complete(Unit)
        runCurrent()
        assertEquals(ShotStatus.UPLOADED, queue.shot().status)
    }

    @Test
    fun `a transient failure is retried with back-off and the same capture_id`() = runTest {
        val client = ScriptedUploadClient(
            BackendResult.Unreachable("timeout"),
            BackendResult.HttpError(503),
            BackendResult.Unreachable("timeout"),
            stored(status = CaptureUploadStatus.DUPLICATE),
        )
        val queue = queue(client, delays = listOf(1_000, 2_000))
        queue.take()
        runCurrent()
        assertEquals(1, client.uploads.size)
        assertEquals(ShotStatus.PENDING, queue.shot().status)
        assertEquals(1, queue.shot().failedAttempts)
        assertEquals(BackendResult.Unreachable("timeout"), queue.shot().lastFailure)

        advanceTimeBy(999)
        runCurrent()
        assertEquals(1, client.uploads.size)
        advanceTimeBy(1)
        runCurrent()
        assertEquals(2, client.uploads.size)

        advanceTimeBy(2_000) // second delay
        runCurrent()
        assertEquals(3, client.uploads.size)
        advanceTimeBy(2_000) // the last delay repeats
        runCurrent()
        assertEquals(4, client.uploads.size)

        assertEquals(listOf("c-1"), client.uploads.map { it.metadata.captureId }.distinct())
        assertEquals(1, client.uploads.map { it.metadata }.distinct().size)
        assertEquals(ShotStatus.UPLOADED, queue.shot().status)
        assertNull(queue.shot().lastFailure)
    }

    @Test
    fun `a permanent refusal fails at once and a manual retry resends the same capture`() = runTest {
        val client = ScriptedUploadClient(BackendResult.HttpError(422), stored())
        val queue = queue(client)
        queue.take()
        runCurrent()
        advanceTimeBy(60_000)
        runCurrent()
        assertEquals(1, client.uploads.size)
        assertEquals(ShotStatus.FAILED, queue.shot().status)
        assertEquals(BackendResult.HttpError(422), queue.shot().lastFailure)

        queue.retry("c-1")
        runCurrent()
        assertEquals(2, client.uploads.size)
        assertEquals(client.uploads[0].metadata, client.uploads[1].metadata)
        assertEquals(ShotStatus.UPLOADED, queue.shot().status)

        queue.retry("c-1") // an uploaded capture is not sent again
        runCurrent()
        assertEquals(2, client.uploads.size)
    }

    @Test
    fun `conflicts are retried a bounded number of times`() = runTest {
        val client = ScriptedUploadClient(BackendResult.HttpError(409))
        val queue = queue(client, delays = listOf(10), conflicts = 3)
        queue.take()
        advanceTimeBy(10_000)
        runCurrent()
        assertEquals(3, client.uploads.size)
        assertEquals(ShotStatus.FAILED, queue.shot().status)
        assertEquals(3, queue.shot().failedAttempts)
    }

    @Test
    fun `transient and permanent failures are classified`() {
        val transient = listOf(
            BackendResult.Unreachable("x"),
            BackendResult.InvalidResponse("x"),
            BackendResult.HttpError(408),
            BackendResult.HttpError(409),
            BackendResult.HttpError(429),
            BackendResult.HttpError(500),
            BackendResult.HttpError(503),
        )
        val permanent = listOf(
            BackendResult.HttpError(400),
            BackendResult.HttpError(401),
            BackendResult.HttpError(403),
            BackendResult.HttpError(404),
            BackendResult.HttpError(413),
            BackendResult.HttpError(422),
            BackendResult.IncompatibleVersion("2.0", "1.1"),
        )
        transient.forEach { assertEquals(it.toString(), true, CaptureUploadQueue.isTransient(it)) }
        permanent.forEach { assertEquals(it.toString(), false, CaptureUploadQueue.isTransient(it)) }
    }

    @Test
    fun `uploads run one at a time, in order`() = runTest {
        val client = ScriptedUploadClient(stored()).apply { gate = CompletableDeferred() }
        val queue = queue(client)
        queue.take("c-1")
        queue.take("c-2")
        runCurrent()
        assertEquals(listOf("c-1"), client.uploads.map { it.metadata.captureId })
        assertEquals(ShotStatus.PENDING, queue.shot("c-2").status)
        client.gate!!.complete(Unit)
        runCurrent()
        assertEquals(listOf("c-1", "c-2"), client.uploads.map { it.metadata.captureId })
    }

    @Test
    fun `a camera failure shows without uploading, and shots are per session`() = runTest {
        val client = ScriptedUploadClient(stored())
        val queue = queue(client)
        queue.begin("c-1", backend, "s1", CaptureTrigger.BUTTON, null, 1_000)
        queue.cameraFailed("c-1")
        queue.begin("c-2", backend, "s2", CaptureTrigger.BUTTON, null, 1_100)
        runCurrent()
        assertEquals(ShotStatus.CAMERA_FAILED, queue.shot("c-1").status)
        assertEquals(emptyList<ScriptedUploadClient.Upload>(), client.uploads)
        assertEquals(listOf("c-1"), queue.shots("s1").first().map { it.captureId })
        assertEquals(listOf("c-2"), queue.shots("s2").first().map { it.captureId })
    }
}
