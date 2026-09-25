package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.protocol.NotesGenerationStart
import com.titanarq.studentassistant.protocol.NotesGenerationState
import com.titanarq.studentassistant.protocol.NotesGenerationStatus
import kotlinx.coroutines.delay

/**
 * What the capture screen shows after "Terminar y preparar apuntes" (#272): the background notes
 * generation the end started (protocol 1.6), as `GET .../notes/generation` reports it.
 */
sealed interface NotesProgress {
    /** Opus is writing the notes. [pollFailure]: the last poll got no answer (polling goes on). */
    data class Running(val pollFailure: BackendResult.Failure? = null) : NotesProgress

    /** The notes are written ([draft]: a draft awaits review), with an optional Spanish [warning]. */
    data class Done(val version: Int?, val draft: Boolean, val warning: String?) : NotesProgress

    /** Claude refused or failed, or a server error; the notes are untouched. */
    data class Failed(val detail: String?) : NotesProgress

    /** A spending cap is reached and nothing was spent: the student confirms from the study desk. */
    data class NeedsConfirmation(val detail: String?) : NotesProgress

    /** The backend cannot prepare notes (no Claude, or it ignored `prepare_notes`). */
    data object Unavailable : NotesProgress

    /** The backend knows of no generation of the topic (it restarted meanwhile). */
    data object Lost : NotesProgress

    /** Polling was refused for good (401, 404, an incompatible version, ...). */
    data class Unknown(val failure: BackendResult.Failure) : NotesProgress

    /** Whether nothing will change any more: polling stops. */
    val finished: Boolean get() = this !is Running

    companion object {
        /** What the end response's `notes_generation` means before the first poll (null: an older backend). */
        fun fromStart(start: NotesGenerationStart?): NotesProgress = when (start) {
            NotesGenerationStart.STARTED, NotesGenerationStart.RUNNING -> Running()
            NotesGenerationStart.UNAVAILABLE, null -> Unavailable
        }

        fun fromStatus(status: NotesGenerationStatus): NotesProgress = when (status.status) {
            NotesGenerationState.RUNNING -> Running()
            NotesGenerationState.DONE -> Done(status.version, status.draft == true, status.warning)
            NotesGenerationState.FAILED -> Failed(status.detail)
            NotesGenerationState.NEEDS_CONFIRMATION -> NeedsConfirmation(status.detail)
            NotesGenerationState.IDLE -> Lost
        }
    }
}

/**
 * Polls a topic's notes generation every [intervalMs] until it is no longer running. A transient
 * failure (unreachable, 5xx, ...) keeps polling and is reported as [NotesProgress.Running] with its
 * failure; a refusal stops with [NotesProgress.Unknown]. Cancelling the calling coroutine stops it.
 */
class NotesGenerationPoller(
    private val client: BackendClient,
    private val backend: BackendCredentials,
    private val subjectId: String,
    private val topicId: String,
    private val intervalMs: Long = DEFAULT_INTERVAL_MS,
) {
    init {
        require(intervalMs > 0) { "intervalMs must be positive" }
    }

    /** Polls (the first one after [intervalMs]) and hands every progress to [onProgress]; returns the final one. */
    suspend fun poll(onProgress: (NotesProgress) -> Unit): NotesProgress {
        while (true) {
            delay(intervalMs)
            val progress = when (val result = client.notesGeneration(backend, subjectId, topicId)) {
                is BackendResult.Success -> NotesProgress.fromStatus(result.value)
                is BackendResult.Failure ->
                    if (CaptureUploadQueue.isTransient(result)) NotesProgress.Running(result) else NotesProgress.Unknown(result)
            }
            onProgress(progress)
            if (progress.finished) return progress
        }
    }

    companion object {
        /** Opus takes minutes: a poll every few seconds is plenty. */
        const val DEFAULT_INTERVAL_MS: Long = 3_000
    }
}
