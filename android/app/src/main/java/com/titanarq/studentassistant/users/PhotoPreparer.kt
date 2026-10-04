package com.titanarq.studentassistant.users

import android.content.ContentResolver
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.ExifInterface
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream

/** A photo ready to be sent: JPEG bytes. */
class PreparedPhoto(val bytes: ByteArray, val contentType: String = "image/jpeg") {
    override fun toString(): String = "PreparedPhoto(${bytes.size} bytes, $contentType)"
}

/**
 * Turns the image the Photo Picker returned into the photo to upload (#555): decoded (sampled
 * down), rotated by its EXIF orientation, scaled to at most [PHOTO_MAX_EDGE_PX] on the long edge
 * and re-encoded as JPEG at [PHOTO_JPEG_QUALITY], all off the main thread. Null when the image
 * cannot be read.
 */
class PhotoPreparer(private val resolver: ContentResolver) {
    suspend fun prepare(uri: Uri): PreparedPhoto? = withContext(Dispatchers.Default) {
        try {
            decode(uri)
        } catch (e: java.io.IOException) {
            null
        } catch (e: SecurityException) {
            null
        } catch (e: OutOfMemoryError) {
            null
        }
    }

    private fun decode(uri: Uri): PreparedPhoto? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) } ?: return null
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        val orientation = resolver.openInputStream(uri)?.use {
            ExifInterface(it).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
        } ?: ExifInterface.ORIENTATION_NORMAL

        val options = BitmapFactory.Options().apply { inSampleSize = photoSampleSize(bounds.outWidth, bounds.outHeight) }
        val decoded = resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, options) } ?: return null

        val target = photoTargetSize(decoded.width, decoded.height, orientation)
        val transform = exifTransform(orientation)
        val turned = transform.rotationDegrees % 180 != 0
        val matrix = Matrix().apply {
            postRotate(transform.rotationDegrees.toFloat())
            if (transform.flipHorizontal) postScale(-1f, 1f)
            val uprightWidth = if (turned) decoded.height else decoded.width
            postScale(target.width.toFloat() / uprightWidth, target.width.toFloat() / uprightWidth)
        }
        val upright = Bitmap.createBitmap(decoded, 0, 0, decoded.width, decoded.height, matrix, true)
        if (upright !== decoded) decoded.recycle()
        val out = ByteArrayOutputStream()
        val ok = upright.compress(Bitmap.CompressFormat.JPEG, PHOTO_JPEG_QUALITY, out)
        upright.recycle()
        return if (ok) PreparedPhoto(out.toByteArray()) else null
    }
}
