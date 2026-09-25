@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadResponse
import com.titanarq.studentassistant.protocol.CaptureUploadStatus
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BurstStillCaptureTest {
    private val backend = BackendCredentials("http://pc:8000", "sa_tok")
    private val clock = FakeClock(7_000)
    private val client = ScriptedUploadClient(
        BackendResult.Success(CaptureUploadResponse("x", "s1", CaptureUploadStatus.STORED, 3, 8_000)),
    )
    private var shutters = 0
    private val counts = mutableListOf<Int>()
    private var ids = 0

    private fun still(n: Int) = Still(CaptureImageBytes(byteArrayOf(n.toByte())), "image/jpeg", 10, 20, 7_000L + n)

    private fun TestScope.capture(camera: StillCamera): Pair<BurstStillCapture, CaptureUploadQueue> {
        val queue = CaptureUploadQueue(backgroundScope, client, listOf(1_000))
        return BurstStillCapture(
            backend = backend,
            sessionId = "s1",
            camera = camera,
            feedback = { shutters++ },
            uploads = queue,
            clock = clock,
            scope = backgroundScope,
            newCaptureId = { "00000000-0000-4000-8000-00000000000${ids++}" },
        ) to queue
    }

    @Test
    fun `a trigger gives feedback and a strip entry at once, then uploads a burst of three`() = runTest {
        val release = CompletableDeferred<Unit>()
        val (capture, queue) = capture { count ->
            counts += count
            release.await()
            Burst((0 until count).map(::still), CaptureImageBytes(byteArrayOf(9)))
        }
        capture.capture(CaptureTrigger.COMMAND, "cmd-1")

        assertEquals(1, shutters)
        val shot = queue.all.value.single()
        assertEquals("00000000-0000-4000-8000-000000000000", shot.captureId)
        assertEquals(ShotStatus.CAPTURING, shot.status)
        assertEquals(7_000L, shot.clientTimeMs)
        assertEquals(CaptureTrigger.COMMAND, shot.trigger)

        release.complete(Unit)
        runCurrent()
        assertEquals(listOf(BurstStillCapture.BURST_SIZE), counts)
        val upload = client.uploads.single()
        assertEquals("cmd-1", upload.metadata.commandId)
        assertEquals(7_000L, upload.metadata.clientTimeMs)
        assertEquals(3, upload.metadata.images.size)
        assertEquals(ShotStatus.UPLOADED, queue.all.value.single().status)
    }

    @Test
    fun `every trigger gets its own capture_id`() = runTest {
        val (capture, queue) = capture { count -> Burst((0 until count).map(::still), null) }
        capture.capture(CaptureTrigger.BUTTON, null)
        capture.capture(CaptureTrigger.BUTTON, null)
        runCurrent()
        assertEquals(2, queue.all.value.map { it.captureId }.distinct().size)
        assertEquals(2, client.uploads.map { it.metadata.captureId }.distinct().size)
    }

    @Test
    fun `a camera that takes nothing marks the capture and uploads nothing`() = runTest {
        val (capture, queue) = capture(NoStillCamera)
        capture.capture(CaptureTrigger.BUTTON, null)
        runCurrent()
        assertEquals(ShotStatus.CAMERA_FAILED, queue.all.value.single().status)
        assertTrue(client.uploads.isEmpty())
        assertEquals(1, shutters)
    }
}
