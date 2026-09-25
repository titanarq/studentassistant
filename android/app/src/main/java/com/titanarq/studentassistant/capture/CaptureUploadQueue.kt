package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
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
 * In memory only: the disk spool of android-offline (#53) is what survives a process restart.
 */
class CaptureUploadQueue(
    private val scope: CoroutineScope,
    private val client: BackendClient,
    private val retryDelaysMs: List<Long> = DEFAULT_RETRY_DELAYS_MS,
    private val maxConflictAttempts: Int = MAX_CONFLICT_ATTEMPTS,
) {
    init {
        require(retryDelaysMs.isNotEmpty()) { "retryDelaysMs must not be empty" }
    }

    private class Entry(
        val shot: CaptureShot,
        val backend: BackendCredentials,
        val commandId: String?,
        val stills: List<Still>? = null,
    ) {
        fun with(shot: CaptureShot, stills: List<Still>? = this.stills) = Entry(shot, backend, commandId, stills)
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

    /** The burst of [captureId] is taken: queue its upload. */
    fun submit(captureId: String, burst: Burst) {
        var queued = false
        mutate { list ->
            list.map { entry ->
                if (entry.shot.captureId != captureId || entry.shot.status != ShotStatus.CAPTURING) {
                    entry
                } else {
                    queued = true
                    entry.with(entry.shot.copy(status = ShotStatus.PENDING, thumbnail = burst.thumbnail), burst.stills)
                }
            }
        }
        if (queued) launchUpload(captureId)
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
            if (entry.shot.status == ShotStatus.FAILED && entry.stills != null) {
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
                val stills = entry.stills ?: return@launch
                setStatus(captureId, ShotStatus.UPLOADING)
                client.uploadCapture(
                    entry.backend,
                    entry.shot.sessionId,
                    request(entry, stills),
                    stills.map { it.bytes },
                )
            }
            if (result is BackendResult.Success) {
                update(captureId) { it.with(it.shot.copy(status = ShotStatus.UPLOADED, lastFailure = null), stills = null) }
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

    private fun request(entry: Entry, stills: List<Still>) = CaptureUploadRequest(
        captureId = entry.shot.captureId,
        trigger = entry.shot.trigger,
        commandId = entry.commandId,
        clientTimeMs = entry.shot.clientTimeMs,
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

        internal fun isTransient(failure: BackendResult.Failure): Boolean = when (failure) {
            is BackendResult.Unreachable -> true
            is BackendResult.InvalidResponse -> true
            is BackendResult.IncompatibleVersion -> false
            is BackendResult.HttpError -> failure.status in TRANSIENT_STATUSES || failure.status >= 500
        }
    }
}
