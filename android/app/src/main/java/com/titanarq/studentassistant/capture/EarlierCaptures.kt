package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes

/**
 * Fills the capture strip with a topic's earlier photos from the backend (#580): lists
 * `GET .../captures`, downloads the thumbnail of each one the [ThumbnailStore] does not hold yet
 * and adds it (as uploaded). Best effort: any failure leaves the store as it is, because the
 * strip must work offline with what this process already knows.
 */
class EarlierCaptures(private val backend: BackendClient, private val store: ThumbnailStore) {
    suspend fun load(credentials: BackendCredentials, key: ThumbnailKey) {
        val listing = (backend.listTopicCaptures(credentials, key.subjectId, key.topicId) as? BackendResult.Success)
            ?.value ?: return
        for (capture in listing.captures) {
            if (store.list(key).any { it.captureId == capture.captureId }) continue
            val bytes = (backend.captureThumbnail(credentials, capture.thumbnailUrl) as? BackendResult.Success)
                ?.value ?: continue
            // A photo taken meanwhile in this process may have been added: never replace it.
            if (store.list(key).any { it.captureId == capture.captureId }) continue
            store.add(
                key,
                StoredThumbnail(
                    captureId = capture.captureId,
                    sessionId = capture.sessionId.orEmpty(),
                    clientTimeMs = capture.capturedAtMs,
                    status = ShotStatus.UPLOADED,
                    jpeg = CaptureImageBytes(bytes),
                ),
            )
        }
    }
}
