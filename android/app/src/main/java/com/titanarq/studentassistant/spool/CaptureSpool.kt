package com.titanarq.studentassistant.spool

import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.ProtocolJson
import java.io.File
import java.io.IOException
import kotlinx.serialization.SerialName
import kotlinx.serialization.SerializationException
import kotlinx.serialization.Serializable

/**
 * What is kept of a capture burst until the backend confirms it: the upload's `metadata` part,
 * the session and backend it belongs to (the backend by base URL only; its token is looked up in
 * the paired backends when the upload is resumed) and whether a thumbnail was stored.
 */
@Serializable
data class SpooledCaptureMeta(
    @SerialName("session_id") val sessionId: String,
    @SerialName("base_url") val baseUrl: String,
    val request: CaptureUploadRequest,
    @SerialName("has_thumbnail") val hasThumbnail: Boolean = false,
) {
    val captureId: String get() = request.captureId
}

/**
 * The pending capture bursts, one directory per `capture_id` under [dir] (app-private storage):
 * `image_<n>` (the bytes of `request.images[n]`), `thumbnail` and `meta.json`, written last, so a
 * directory without it (a crash mid-write) is incomplete and deleted on the next [list]. Every byte
 * counts in [budget]; captures are never dropped to make room. Thread-safe.
 */
class CaptureSpool(private val dir: File, private val budget: SpoolBudget) {
    init {
        dir.mkdirs()
        synchronized(this) {
            for (capture in captureDirs()) {
                if (File(capture, META_FILE).isFile) budget.add(sizeOf(capture)) else capture.deleteRecursively()
            }
        }
    }

    /** Stores a burst. Returns false (nothing kept) when it could not be written. */
    fun put(meta: SpooledCaptureMeta, images: List<CaptureImageBytes>, thumbnail: CaptureImageBytes?): Boolean = synchronized(this) {
        require(images.size == meta.request.images.size) { "one image per metadata entry" }
        val target = captureDir(meta.captureId) ?: return false
        if (File(target, META_FILE).isFile) return true
        try {
            target.mkdirs()
            images.forEachIndexed { index, image -> File(target, "$IMAGE_PREFIX$index").writeBytes(image.bytes) }
            if (thumbnail != null) File(target, THUMBNAIL_FILE).writeBytes(thumbnail.bytes)
            val stored = meta.copy(hasThumbnail = thumbnail != null)
            val temp = File(target, "$META_FILE.tmp")
            temp.writeText(ProtocolJson.encodeToString(SpooledCaptureMeta.serializer(), stored))
            if (!temp.renameTo(File(target, META_FILE))) throw IOException("cannot rename $temp")
        } catch (e: IOException) {
            target.deleteRecursively()
            return false
        }
        budget.add(sizeOf(target))
        true
    }

    /** The complete stored captures, oldest trigger first. */
    fun list(): List<SpooledCaptureMeta> = synchronized(this) {
        captureDirs().mapNotNull { capture ->
            val meta = readMeta(capture)
            if (meta == null) {
                budget.add(-sizeOf(capture))
                capture.deleteRecursively()
            }
            meta
        }.sortedBy { it.request.clientTimeMs }
    }

    /** The stored images of [captureId], in `request.images` order, or null when not stored. */
    fun images(captureId: String): List<CaptureImageBytes>? = synchronized(this) {
        val target = captureDir(captureId) ?: return null
        val meta = readMeta(target) ?: return null
        try {
            meta.request.images.indices.map { CaptureImageBytes(File(target, "$IMAGE_PREFIX$it").readBytes()) }
        } catch (e: IOException) {
            null
        }
    }

    fun thumbnail(captureId: String): CaptureImageBytes? = synchronized(this) {
        val file = captureDir(captureId)?.let { File(it, THUMBNAIL_FILE) } ?: return null
        try {
            if (file.isFile) CaptureImageBytes(file.readBytes()) else null
        } catch (e: IOException) {
            null
        }
    }

    fun contains(captureId: String): Boolean = synchronized(this) {
        captureDir(captureId)?.let { File(it, META_FILE).isFile } == true
    }

    /** The backend has [captureId] (or it is given up): its files are deleted. */
    fun remove(captureId: String): Unit = synchronized(this) {
        val target = captureDir(captureId) ?: return
        if (!target.exists()) return
        budget.add(-sizeOf(target))
        target.deleteRecursively()
    }

    private fun readMeta(capture: File): SpooledCaptureMeta? = try {
        ProtocolJson.decodeFromString(SpooledCaptureMeta.serializer(), File(capture, META_FILE).readText())
    } catch (e: IOException) {
        null
    } catch (e: SerializationException) {
        null
    } catch (e: IllegalArgumentException) {
        null
    }

    private fun captureDirs(): List<File> = dir.listFiles().orEmpty().filter { it.isDirectory }

    /** Null for an id that is not a safe file name (the app's ids are UUIDs). */
    private fun captureDir(captureId: String): File? =
        if (SAFE_ID.matches(captureId)) File(dir, captureId) else null

    private fun sizeOf(capture: File): Long = capture.walkBottomUp().filter { it.isFile }.sumOf { it.length() }

    private companion object {
        const val META_FILE = "meta.json"
        const val THUMBNAIL_FILE = "thumbnail"
        const val IMAGE_PREFIX = "image_"
        val SAFE_ID = Regex("[A-Za-z0-9_-]{1,128}")
    }
}
