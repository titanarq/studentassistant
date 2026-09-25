package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.CaptureImageBytes

/** One full-resolution still of a burst, ready to upload as an `image_N` part. */
class Still(
    val bytes: CaptureImageBytes,
    /** `image/jpeg`, `image/png` or `image/webp` (protocol v1). */
    val contentType: String,
    /** As displayed, i.e. after the still's EXIF orientation. */
    val widthPx: Int,
    val heightPx: Int,
    /** Client clock when this still was taken, Unix epoch ms. */
    val clientTimeMs: Long,
) {
    override fun toString(): String = "Still($contentType ${widthPx}x$heightPx at $clientTimeMs, $bytes)"
}

/** What a burst produced: its stills (at least one) and a small JPEG for the thumbnail strip. */
class Burst(val stills: List<Still>, val thumbnail: CaptureImageBytes?) {
    init {
        require(stills.isNotEmpty()) { "a burst holds at least one still" }
    }
}

/** The camera could not take any still (not bound, closed, failed). */
class StillCameraException(message: String, cause: Throwable? = null) : Exception(message, cause)

/**
 * The camera side of still capture: takes up to [count] stills in a row. [CameraXStillCamera] on a
 * device; tests use a fake. A burst whose later stills fail returns the ones it got; one that got
 * none throws [StillCameraException].
 */
fun interface StillCamera {
    suspend fun takeBurst(count: Int): Burst
}

/** The placeholder [StillCamera] of a container built without a camera: always fails. */
object NoStillCamera : StillCamera {
    override suspend fun takeBurst(count: Int): Burst = throw StillCameraException("no camera configured")
}

/** Immediate feedback that a capture was triggered: vibration and shutter sound on a device. */
fun interface CaptureFeedback {
    fun shutter()
}

/** Gives no feedback (tests, and a container built without one). */
object NoCaptureFeedback : CaptureFeedback {
    override fun shutter() = Unit
}
