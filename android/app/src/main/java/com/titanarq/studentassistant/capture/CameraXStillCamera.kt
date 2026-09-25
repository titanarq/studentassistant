package com.titanarq.studentassistant.capture

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.AudioAttributes
import android.media.ExifInterface
import android.media.MediaActionSound
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.CaptureImageBytes
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * The device [StillCamera]: a CameraX [ImageCapture] use case at the highest available resolution
 * (quality mode, JPEG), which the capture screen binds next to its preview. Each still is written
 * by CameraX as a JPEG with its EXIF orientation; its reported size is the displayed one. Bursts
 * never interleave. The thumbnail is the first still, about [THUMBNAIL_PX] on its long side.
 */
class CameraXStillCamera(private val clock: Clock) : StillCamera {
    private val executor = Executors.newSingleThreadExecutor()
    private val burstLock = Mutex()

    /** Bound by the capture screen with its preview; unbound, a burst fails. */
    val imageCapture: ImageCapture = ImageCapture.Builder()
        .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
        .setResolutionSelector(
            ResolutionSelector.Builder()
                .setResolutionStrategy(ResolutionStrategy.HIGHEST_AVAILABLE_STRATEGY)
                .build(),
        )
        .setFlashMode(ImageCapture.FLASH_MODE_OFF)
        .build()

    override suspend fun takeBurst(count: Int): Burst = burstLock.withLock {
        val stills = mutableListOf<Still>()
        var lastError: Exception? = null
        repeat(count) {
            try {
                val takenAt = clock.nowMillis()
                val bytes = takeJpeg()
                stills += withContext(Dispatchers.Default) { describe(bytes, takenAt) }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                lastError = e
            }
        }
        if (stills.isEmpty()) throw StillCameraException("no still taken", lastError)
        Burst(stills, withContext(Dispatchers.Default) { thumbnail(stills.first().bytes.bytes) })
    }

    private suspend fun takeJpeg(): ByteArray = suspendCancellableCoroutine { cont ->
        val out = ByteArrayOutputStream()
        val options = ImageCapture.OutputFileOptions.Builder(out).build()
        imageCapture.takePicture(
            options,
            executor,
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(outputFileResults: ImageCapture.OutputFileResults) {
                    cont.resume(out.toByteArray())
                }

                override fun onError(exception: ImageCaptureException) {
                    cont.resumeWithException(StillCameraException(exception.message ?: "capture failed", exception))
                }
            },
        )
    }

    private fun describe(bytes: ByteArray, takenAt: Long): Still {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) throw StillCameraException("undecodable still")
        val quarterTurn = rotationDegrees(bytes) % 180 != 0
        return Still(
            bytes = CaptureImageBytes(bytes),
            contentType = "image/jpeg",
            widthPx = if (quarterTurn) bounds.outHeight else bounds.outWidth,
            heightPx = if (quarterTurn) bounds.outWidth else bounds.outHeight,
            clientTimeMs = takenAt,
        )
    }

    private fun thumbnail(bytes: ByteArray): CaptureImageBytes? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= THUMBNAIL_PX) sample *= 2
        val decoded = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, BitmapFactory.Options().apply { inSampleSize = sample })
            ?: return null
        val rotation = rotationDegrees(bytes)
        val upright = if (rotation == 0) {
            decoded
        } else {
            Bitmap.createBitmap(decoded, 0, 0, decoded.width, decoded.height, Matrix().apply { postRotate(rotation.toFloat()) }, true)
        }
        val out = ByteArrayOutputStream()
        upright.compress(Bitmap.CompressFormat.JPEG, THUMBNAIL_QUALITY, out)
        if (upright !== decoded) upright.recycle()
        decoded.recycle()
        return CaptureImageBytes(out.toByteArray())
    }

    private fun rotationDegrees(bytes: ByteArray): Int =
        when (ExifInterface(ByteArrayInputStream(bytes)).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
            ExifInterface.ORIENTATION_ROTATE_90, ExifInterface.ORIENTATION_TRANSPOSE -> 90
            ExifInterface.ORIENTATION_ROTATE_180 -> 180
            ExifInterface.ORIENTATION_ROTATE_270, ExifInterface.ORIENTATION_TRANSVERSE -> 270
            else -> 0
        }

    companion object {
        const val THUMBNAIL_PX: Int = 256
        private const val THUMBNAIL_QUALITY = 80
    }
}

/** Vibration and the system shutter sound on every capture trigger. */
class AndroidCaptureFeedback(context: Context) : CaptureFeedback {
    private val vibrator: Vibrator? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        context.getSystemService(VibratorManager::class.java)?.defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        context.getSystemService(Vibrator::class.java)
    }
    private val sound = MediaActionSound().apply { load(MediaActionSound.SHUTTER_CLICK) }

    override fun shutter() {
        sound.play(MediaActionSound.SHUTTER_CLICK)
        val vibrator = vibrator ?: return
        if (!vibrator.hasVibrator()) return
        vibrator.vibrate(
            VibrationEffect.createOneShot(VIBRATION_MS, VibrationEffect.DEFAULT_AMPLITUDE),
            AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANCE_SONIFICATION).build(),
        )
    }

    private companion object {
        const val VIBRATION_MS = 40L
    }
}
