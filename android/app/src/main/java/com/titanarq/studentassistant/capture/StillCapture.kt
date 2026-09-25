package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.protocol.CaptureTrigger
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.launch

/**
 * Takes a burst of stills and uploads them (ADR-0001). The capture screen calls [capture] on
 * "Capturar" ([CaptureTrigger.BUTTON]) and on a `capture_now` command ([CaptureTrigger.COMMAND]
 * with its `command_id`); [shots] feeds the thumbnail strip.
 */
interface StillCapture {
    /** This session's captures, oldest first. */
    val shots: Flow<List<CaptureShot>>

    fun capture(trigger: CaptureTrigger, commandId: String?)

    /** Uploads a [ShotStatus.FAILED] capture again (same `capture_id`). */
    fun retry(captureId: String) = Unit

    /** The backend holds [captureIds] (a WebSocket `ack`'s `capture_ids`): never upload them again. */
    fun confirmReceived(captureIds: List<String>) = Unit

    /**
     * The session was started or resumed and the backend holds [receivedCaptureIds]: those are
     * confirmed, and captures that failed for good are tried again.
     */
    fun resumed(receivedCaptureIds: List<String>) = Unit

    /** Suspends until no capture of this session is being taken or waiting to upload. */
    suspend fun awaitUploads() = Unit
}

/** The placeholder [StillCapture]: takes nothing. */
object NoStillCapture : StillCapture {
    override val shots: Flow<List<CaptureShot>> = flowOf(emptyList())

    override fun capture(trigger: CaptureTrigger, commandId: String?) = Unit
}

/**
 * The real [StillCapture] for one session: on a trigger it gives [feedback] and adds the capture
 * to the strip at once (with a fresh UUID `capture_id` and the phone time of the trigger), then
 * takes a burst of [burstSize] stills with [camera] on [scope] and hands it to [uploads], which
 * uploads it in the background with retries.
 */
class BurstStillCapture(
    private val backend: BackendCredentials,
    private val sessionId: String,
    private val camera: StillCamera,
    private val feedback: CaptureFeedback,
    private val uploads: CaptureUploadQueue,
    private val clock: Clock,
    private val scope: CoroutineScope,
    private val burstSize: Int = BURST_SIZE,
    private val newCaptureId: () -> String = { UUID.randomUUID().toString() },
) : StillCapture {
    override val shots: Flow<List<CaptureShot>> = uploads.shots(sessionId)

    override fun capture(trigger: CaptureTrigger, commandId: String?) {
        val captureId = newCaptureId()
        feedback.shutter()
        uploads.begin(captureId, backend, sessionId, trigger, commandId, clock.nowMillis())
        scope.launch {
            val burst = try {
                camera.takeBurst(burstSize)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                null
            }
            if (burst == null) uploads.cameraFailed(captureId) else uploads.submit(captureId, burst)
        }
    }

    override fun retry(captureId: String) = uploads.retry(captureId)

    override fun confirmReceived(captureIds: List<String>) = uploads.confirmReceived(sessionId, captureIds)

    override fun resumed(receivedCaptureIds: List<String>) {
        uploads.confirmReceived(sessionId, receivedCaptureIds)
        uploads.retryFailed(sessionId)
    }

    override suspend fun awaitUploads() {
        uploads.all.first { !uploads.hasPending(sessionId) }
    }

    companion object {
        /** Stills per capture; the backend keeps the sharpest (#44). */
        const val BURST_SIZE: Int = 3
    }
}
