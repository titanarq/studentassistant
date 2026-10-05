package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.CaptureTrigger
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.update

/** The topic a thumbnail belongs to. */
data class ThumbnailKey(val subjectId: String, val topicId: String)

/**
 * One photo thumbnail kept for the capture strip: the downscaled JPEG the camera made (a few KiB,
 * see `CameraXStillCamera.THUMBNAIL_PX`), never the full-size stills.
 */
class StoredThumbnail(
    val captureId: String,
    /** The session the photo was taken in. */
    val sessionId: String,
    /** Phone time of the trigger, Unix epoch ms. */
    val clientTimeMs: Long,
    val status: ShotStatus,
    val jpeg: CaptureImageBytes,
) {
    /** The strip entry for this photo. */
    fun toShot() = CaptureShot(captureId, sessionId, CaptureTrigger.BUTTON, clientTimeMs, status, jpeg)

    companion object {
        /** What the strip keeps of [shot]; null while it has no thumbnail (or the camera failed). */
        fun of(shot: CaptureShot): StoredThumbnail? {
            val jpeg = shot.thumbnail ?: return null
            if (shot.status == ShotStatus.CAMERA_FAILED || shot.status == ShotStatus.CAPTURING) return null
            return StoredThumbnail(shot.captureId, shot.sessionId, shot.clientTimeMs, shot.status, jpeg)
        }
    }
}

/**
 * The thumbnails of the photos taken per (subject, topic) while the app process lives (#583), so the
 * capture strip is not empty when the student re-enters a topic. In memory only (it lives in
 * [com.titanarq.studentassistant.AppContainer]); [clear] runs when the user changes. Memory is
 * bounded: thumbnails are small and a topic keeps at most [maxPerTopic] (the oldest are dropped).
 *
 * Meant to be extended: a later task discards a photo by dragging it down ([remove]), and the
 * persistent listing of a topic's earlier captures (#580, [EarlierCaptures]) [add]s what the
 * backend lists.
 */
class ThumbnailStore(private val maxPerTopic: Int = MAX_PER_TOPIC) {
    init {
        require(maxPerTopic > 0) { "maxPerTopic must be positive" }
    }

    private val state = MutableStateFlow<Map<ThumbnailKey, List<StoredThumbnail>>>(emptyMap())

    /** Every topic's thumbnails, oldest first. */
    val all: StateFlow<Map<ThumbnailKey, List<StoredThumbnail>>> = state.asStateFlow()

    /** The thumbnails of [key], oldest first. */
    fun list(key: ThumbnailKey): List<StoredThumbnail> = state.value[key].orEmpty()

    /** [list] as a flow that emits on every change of [key]'s thumbnails. */
    fun flow(key: ThumbnailKey): Flow<List<StoredThumbnail>> = state.map { it[key].orEmpty() }.distinctUntilChanged()

    /** Adds [thumbnail], or replaces the one with the same capture id (its status changed); the list stays ordered by photo time (earlier photos can arrive late, #580). */
    fun add(key: ThumbnailKey, thumbnail: StoredThumbnail) {
        state.update { map ->
            val current = map[key].orEmpty()
            val index = current.indexOfFirst { it.captureId == thumbnail.captureId }
            val next = if (index >= 0) current.toMutableList().also { it[index] = thumbnail } else (current + thumbnail).sortedBy { it.clientTimeMs }
            map + (key to next.takeLast(maxPerTopic))
        }
    }

    /** Forgets [captureId] of [key]. */
    fun remove(key: ThumbnailKey, captureId: String) {
        state.update { map ->
            val next = map[key].orEmpty().filterNot { it.captureId == captureId }
            if (next.isEmpty()) map - key else map + (key to next)
        }
    }

    /** Replaces everything of [key] with [thumbnails] (oldest first). */
    fun replace(key: ThumbnailKey, thumbnails: List<StoredThumbnail>) {
        state.update { map -> if (thumbnails.isEmpty()) map - key else map + (key to thumbnails.takeLast(maxPerTopic)) }
    }

    /** Forgets everything (profile switch, sign-out). */
    fun clear() {
        state.value = emptyMap()
    }

    companion object {
        /** Thumbnails kept per topic. */
        const val MAX_PER_TOPIC: Int = 40
    }
}
