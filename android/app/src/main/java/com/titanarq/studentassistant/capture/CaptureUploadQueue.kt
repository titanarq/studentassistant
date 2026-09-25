package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.spool.CaptureSpool
import com.titanarq.studentassistant.spool.SpooledCaptureMeta
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit

/** Where a capture is on its way to the backend. */
enum class ShotStatus {
    /** Triggered; the burst is being taken. */
    CAPTURING,

    /** Taken, waiting to upload (first try, or a retry after a failure). */
    PENDING,

    UPLOADING,

    /** The backend has it (`stored`, or `duplicate` on a retry). */
    UPLOADED,

    /** The backend refused it for good (or conflicts outlasted the retries); [CaptureUploadQueue.retry] tries again. */
    FAILED,

    /** The camera took no still; nothing to upload. */
    CAMERA_FAILED,
}

/** One entry of the thumbnail strip. */
data class CaptureShot(
    val captureId: String,
    val sessionId: String,
    val trigger: CaptureTrigger,
    /** Phone time of the trigger, Unix epoch ms. */
    val clientTimeMs: Long,
    val status: ShotStatus,
    /** A small JPEG of the first still, once the burst is taken. */
    val thumbnail: CaptureImageBytes? = null,
    /** Failed upload attempts so far. */
    val failedAttempts: Int = 0,
    /** The last failed attempt's outcome. */
    val lastFailure: BackendResult.Failure? = null,
)

/**
 * The app-wide upload queue of capture bursts (`POST /api/sessions/{id}/captures`). It lives on an
 * app scope, so an upload goes on when the capture screen is left.
 *
 * Each burst is uploaded as one multipart request whose `capture_id` never changes, so a retry is
 * idempotent (the backend answers `duplicate` to one it already stored). Uploads run one at a
 * time. A transient failure -- no answer, a 408/425/429/5xx, a 2xx the client could not read, or a
 * 409 (the session may be resuming after a backend restart; at most [maxConflictAttempts] times)
 * -- is retried after [retryDelaysMs] (the last delay repeats); any other refusal (400, 401, 403,
 * 404, 413, 422, an incompatible version) marks the capture [ShotStatus.FAILED]. The full-size
 * stills are dropped once uploaded; only the thumbnail stays.
 *
 * With a [spool], every taken burst is written to disk before its first upload and deleted once
 * the backend has it, so an unsent capture survives the app process dying: [restore] queues the
 * spooled ones again at the next start, and the stills are read back from disk only for the
 * upload. Without one (tests) the stills stay in memory. [confirmReceived] takes the
 * `received_capture_ids` of a session resume (or a WebSocket `ack`'s `capture_ids`): those are
 * uploaded already and are never sent again.
 */
class CaptureUploadQueue(
    private val scope: CoroutineScope,
    private val client: BackendClient,
    private val retryDelaysMs: List<Long> = DEFAULT_RETRY_DELAYS_MS,
    private val maxConflictAttempts: Int = MAX_CONFLICT_ATTEMPTS,
    private val spool: CaptureSpool? = null,
) {
    init {
        require(retryDelaysMs.isNotEmpty()) { "retryDelaysMs must not be empty" }
    }

    private class Entry(
        val shot: CaptureShot,
        val backend: BackendCredentials,
        val commandId: String?,
        /** The upload's metadata, once the burst is taken. */
        val request: CaptureUploadRequest? = null,
        /** The stills' bytes when kept in memory; null when they are on disk (or uploaded). */
        val images: List<CaptureImageBytes>? = null,
    ) {
        fun with(shot: CaptureShot, images: List<CaptureImageBytes>? = this.images) =
            Entry(shot, backend, commandId, request, images)
    }

    private val entries = MutableStateFlow<List<Entry>>(emptyList())
    private val uploadPermit = Semaphore(1)

    private val allShots = MutableStateFlow<List<CaptureShot>>(emptyList())

    /** Every capture of every session, oldest first. */
    val all: StateFlow<List<CaptureShot>> = allShots.asStateFlow()

    /** The captures of [sessionId], oldest first. */
    fun shots(sessionId: String): Flow<List<CaptureShot>> =
        allShots.map { shots -> shots.filter { it.sessionId == sessionId } }.distinctUntilChanged()

    /** A capture was triggered: it shows as [ShotStatus.CAPTURING] until [submit] or [cameraFailed]. */
    fun begin(
        captureId: String,
        backend: BackendCredentials,
        sessionId: String,
        trigger: CaptureTrigger,
        commandId: String?,
        clientTimeMs: Long,
    ) {
        val shot = CaptureShot(captureId, sessionId, trigger, clientTimeMs, ShotStatus.CAPTURING)
        mutate { list -> if (list.any { it.shot.captureId == captureId }) list else list + Entry(shot, backend, commandId) }
    }

    /** The burst of [captureId] is taken: spool it and queue its upload. */
    fun submit(captureId: String, burst: Burst) {
        val entry = find(captureId)?.takeIf { it.shot.status == ShotStatus.CAPTURING } ?: return
        val request = request(entry.shot, entry.commandId, burst.stills)
        val images = burst.stills.map { it.bytes }
        val onDisk = spool?.put(
            SpooledCaptureMeta(entry.shot.sessionId, entry.backend.baseUrl, request),
            images,
            burst.thumbnail,
        ) == true
        var queued = false
        mutate { list ->
            list.map {
                if (it.shot.captureId != captureId || it.shot.status != ShotStatus.CAPTURING) {
                    it
                } else {
                    queued = true
                    Entry(
                        it.shot.copy(status = ShotStatus.PENDING, thumbnail = burst.thumbnail),
                        it.backend,
                        it.commandId,
                        request,
                        if (onDisk) null else images,
                    )
                }
            }
        }
        if (queued) launchUpload(captureId)
    }

    /**
     * Queues again every capture left in the spool by an earlier run of the app (oldest first),
     * with the credentials [credentials] gives for its backend's base URL; one whose backend is no
     * longer paired stays on disk untouched.
     */
    suspend fun restore(credentials: suspend (baseUrl: String) -> BackendCredentials?) {
        val spool = spool ?: return
        for (meta in spool.list()) {
            if (find(meta.captureId) != null) continue
            val backend = credentials(meta.baseUrl) ?: continue
            val request = meta.request
            val shot = CaptureShot(
                request.captureId,
                meta.sessionId,
                request.trigger,
                request.clientTimeMs,
                ShotStatus.PENDING,
                thumbnail = if (meta.hasThumbnail) spool.thumbnail(meta.captureId) else null,
            )
            var added = false
            mutate { list ->
                if (list.any { it.shot.captureId == meta.captureId }) {
                    list
                } else {
                    added = true
                    list + Entry(shot, backend, request.commandId, request)
                }
            }
            if (added) launchUpload(meta.captureId)
        }
    }

    /**
     * The backend already holds [captureIds] of [sessionId] (a resume's `received_capture_ids`, a
     * WebSocket `ack`'s `capture_ids`): they count as uploaded and are never sent again.
     */
    fun confirmReceived(sessionId: String, captureIds: Collection<String>) {
        if (captureIds.isEmpty()) return
        val ids = captureIds.toSet()
        val confirmed = mutableListOf<String>()
        mutate { list ->
            list.map { entry ->
                val done = entry.shot.sessionId == sessionId && entry.shot.captureId in ids &&
                    entry.request != null && entry.shot.status != ShotStatus.UPLOADED
                if (done) {
                    confirmed += entry.shot.captureId
                    entry.with(entry.shot.copy(status = ShotStatus.UPLOADED, lastFailure = null), images = null)
                } else {
                    entry
                }
            }
        }
        confirmed.forEach { spool?.remove(it) }
    }

    /** Uploads every [ShotStatus.FAILED] capture of [sessionId] again (the session was resumed). */
    fun retryFailed(sessionId: String) {
        entries.value.filter { it.shot.sessionId == sessionId && it.shot.status == ShotStatus.FAILED }
            .forEach { retry(it.shot.captureId) }
    }

    /** True while a capture of [sessionId] is being taken or waits to upload. */
    fun hasPending(sessionId: String): Boolean = entries.value.any {
        it.shot.sessionId == sessionId && it.shot.status in ACTIVE_STATUSES
    }

    /**
     * [sessionId] has ended on the backend: its captures refused for good can never be uploaded, so
     * their spooled files are deleted (they stay [ShotStatus.FAILED] in [all]).
     */
    fun forgetSession(sessionId: String) {
        entries.value.filter { it.shot.sessionId == sessionId && it.shot.status == ShotStatus.FAILED }
            .forEach { spool?.remove(it.shot.captureId) }
    }

    /** The camera took nothing for [captureId]. */
    fun cameraFailed(captureId: String) {
        update(captureId) { entry ->
            if (entry.shot.status == ShotStatus.CAPTURING) entry.with(entry.shot.copy(status = ShotStatus.CAMERA_FAILED)) else entry
        }
    }

    /** Uploads a [ShotStatus.FAILED] capture again, with the same `capture_id`. */
    fun retry(captureId: String) {
        var requeued = false
        update(captureId) { entry ->
            if (entry.shot.status == ShotStatus.FAILED && entry.request != null) {
                requeued = true
                entry.with(entry.shot.copy(status = ShotStatus.PENDING))
            } else {
                entry
            }
        }
        if (requeued) launchUpload(captureId)
    }

    private fun launchUpload(captureId: String) = scope.launch {
        var failures = 0
        var conflicts = 0
        while (true) {
            val result = uploadPermit.withPermit {
                val entry = find(captureId) ?: return@launch
                if (entry.shot.status == ShotStatus.UPLOADED) return@launch // confirmed meanwhile
                val request = entry.request ?: return@launch
                val images = entry.images ?: spool?.images(captureId)
                if (images == null) {
                    // The spooled files are gone (deleted or unreadable): nothing left to send.
                    update(captureId) { it.with(it.shot.copy(status = ShotStatus.FAILED)) }
                    return@launch
                }
                setStatus(captureId, ShotStatus.UPLOADING)
                client.uploadCapture(entry.backend, entry.shot.sessionId, request, images)
            }
            if (result is BackendResult.Success) {
                update(captureId) { it.with(it.shot.copy(status = ShotStatus.UPLOADED, lastFailure = null), images = null) }
                spool?.remove(captureId)
                return@launch
            }
            val failure = result as BackendResult.Failure
            failures++
            if (failure is BackendResult.HttpError && failure.status == 409) conflicts++
            val retry = isTransient(failure) && conflicts < maxConflictAttempts
            val status = if (retry) ShotStatus.PENDING else ShotStatus.FAILED
            update(captureId) { it.with(it.shot.copy(status = status, failedAttempts = failures, lastFailure = failure)) }
            if (!retry) return@launch
            delay(retryDelaysMs[minOf(failures - 1, retryDelaysMs.lastIndex)])
        }
    }

    private fun request(shot: CaptureShot, commandId: String?, stills: List<Still>) = CaptureUploadRequest(
        captureId = shot.captureId,
        trigger = shot.trigger,
        commandId = commandId,
        clientTimeMs = shot.clientTimeMs,
        images = stills.mapIndexed { index, still ->
            CaptureImage("image_$index", still.contentType, still.widthPx, still.heightPx, still.clientTimeMs)
        },
    )

    private fun find(captureId: String): Entry? = entries.value.firstOrNull { it.shot.captureId == captureId }

    private fun setStatus(captureId: String, status: ShotStatus) =
        update(captureId) { it.with(it.shot.copy(status = status)) }

    private fun update(captureId: String, change: (Entry) -> Entry) =
        mutate { list -> list.map { if (it.shot.captureId == captureId) change(it) else it } }

    private fun mutate(change: (List<Entry>) -> List<Entry>) {
        synchronized(entries) {
            entries.update(change)
            allShots.value = entries.value.map { it.shot }
        }
    }

    companion object {
        /** Back-off between upload attempts; the last one repeats. */
        val DEFAULT_RETRY_DELAYS_MS: List<Long> = listOf(1_000, 2_000, 5_000, 10_000, 30_000)

        /** A session still refused as not active (409) after this many tries is given up. */
        const val MAX_CONFLICT_ATTEMPTS: Int = 10

        private val TRANSIENT_STATUSES = setOf(408, 409, 425, 429)

        private val ACTIVE_STATUSES = setOf(ShotStatus.CAPTURING, ShotStatus.PENDING, ShotStatus.UPLOADING)

        internal fun isTransient(failure: BackendResult.Failure): Boolean = when (failure) {
            is BackendResult.Unreachable -> true
            is BackendResult.InvalidResponse -> true
            is BackendResult.IncompatibleVersion -> false
            is BackendResult.HttpError -> failure.status in TRANSIENT_STATUSES || failure.status >= 500
        }
    }
}
