package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionEndRequest
import com.titanarq.studentassistant.session.PendingEnds
import com.titanarq.studentassistant.spool.PendingEnd
import com.titanarq.studentassistant.spool.Spools
import kotlin.coroutines.CoroutineContext
import kotlin.coroutines.EmptyCoroutineContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Completes the sessions the student ended while the backend could not be told (android-offline,
 * #53): the spool is flushed first, then `POST /api/sessions/{id}/end`, retried until the backend
 * answers. A [PendingEnd] is written to [spools] before anything else, so a pending end survives
 * the app process dying and [restore] picks it up at the next start.
 *
 * For each pending end, on [scope] (app-wide):
 * 1. `POST .../resume`: on success its `received_capture_ids` are confirmed in [uploads] (never
 *    uploaded again) and failed captures of the session are retried; a 409 (already ended, or
 *    another session active) skips the flush; a 404 (no such session) drops everything.
 * 2. When the session's audio or event spool holds something, a [SessionConnection] over those
 *    spools resends it, until [SessionConnection.drained] or [drainTimeoutMs].
 * 3. Waits until no capture of the session is pending upload, at most [capturesTimeoutMs].
 * 4. `POST .../end` with the student's time of "Terminar" (and `prepare_notes` when the student
 *    chose "Terminar y preparar apuntes", so the backend prepares the notes then): success, 404 or 409 (ended already)
 *    completes it, the session's spools are deleted and its captures refused for good forgotten.
 *
 * A transient failure (unreachable, 408/425/429/5xx, an unreadable answer) retries the whole
 * sequence after [retryDelaysMs] (the last delay repeats); a refusal (401, 403, 400, 422, an
 * incompatible version) leaves the pending end on disk for the next start.
 *
 * "Continuar" on the home screen goes through [continueInstead], so a session the student takes up
 * again is never ended behind their back, and never ended twice.
 */
class SessionFinisher(
    private val scope: CoroutineScope,
    private val client: BackendClient,
    private val spools: Spools,
    private val uploads: CaptureUploadQueue,
    private val socketFactory: SessionSocketFactory,
    private val clock: Clock,
    private val credentials: suspend (baseUrl: String) -> BackendCredentials?,
    private val loopContext: CoroutineContext = EmptyCoroutineContext,
    private val retryDelaysMs: List<Long> = DEFAULT_RETRY_DELAYS_MS,
    private val drainTimeoutMs: Long = DRAIN_TIMEOUT_MS,
    private val capturesTimeoutMs: Long = CAPTURES_TIMEOUT_MS,
    private val reconnectDelaysMs: List<Long> = SessionConnection.DEFAULT_RECONNECT_DELAYS_MS,
) : PendingEnds {
    init {
        require(retryDelaysMs.isNotEmpty()) { "retryDelaysMs must not be empty" }
    }

    // All guarded by `jobs`.
    private val jobs = HashMap<String, Job>()
    private val backends = HashMap<String, BackendCredentials>()

    /** Sessions a [continueInstead] is working on: no end is started for them meanwhile. */
    private val held = HashSet<String>()

    /** Sessions the student continued in this process: [restore] leaves them alone. */
    private val continued = HashSet<String>()

    private val _pending = MutableStateFlow<Set<String>>(emptySet())

    /** Session ids whose end is being completed now (a refused one leaves this set, not the disk). */
    override val pending: StateFlow<Set<String>> = _pending.asStateFlow()

    /** Records [end] on disk and completes it in the background. */
    fun finish(backend: BackendCredentials, end: PendingEnd) {
        synchronized(jobs) { continued -= end.sessionId }
        spools.putEnd(end)
        launch(backend, end)
    }

    override suspend fun continueInstead(sessionId: String, resume: suspend () -> Boolean): Boolean {
        val job = synchronized(jobs) {
            held += sessionId
            jobs[sessionId]
        }
        val wasRunning = job?.isActive == true
        var continuing = false
        try {
            job?.cancelAndJoin()
            continuing = resume()
            return continuing
        } finally {
            withContext(NonCancellable) {
                val backend = synchronized(jobs) {
                    held -= sessionId
                    if (continuing) continued += sessionId
                    backends[sessionId]
                }
                if (continuing) {
                    // Also an end refused earlier: the next start must not end a session in use.
                    spools.removeEnd(sessionId)
                } else if (wasRunning) {
                    // The resume failed: the end the student asked for goes on.
                    val end = spools.ends().firstOrNull { it.sessionId == sessionId }
                    val credentials = backend ?: end?.let { credentials(it.baseUrl) }
                    if (end != null && credentials != null) launch(credentials, end)
                }
            }
        }
    }

    /** Resumes every pending end left by an earlier run (its backend looked up by base URL). */
    suspend fun restore() {
        for (end in spools.ends()) {
            val backend = credentials(end.baseUrl) ?: continue
            launch(backend, end)
        }
    }

    /** The session ended on the backend by other means: its spools are deleted. */
    fun ended(sessionId: String) {
        spools.deleteSession(sessionId)
        spools.removeEnd(sessionId)
        uploads.forgetSession(sessionId)
    }

    private fun launch(backend: BackendCredentials, end: PendingEnd) {
        synchronized(jobs) {
            if (end.sessionId in held || end.sessionId in continued) return
            if (jobs[end.sessionId]?.isActive == true) return
            backends[end.sessionId] = backend
            _pending.update { it + end.sessionId }
            jobs[end.sessionId] = scope.launch { run(backend, end) }
        }
    }

    private suspend fun run(backend: BackendCredentials, end: PendingEnd) {
        var failures = 0
        try {
            while (true) {
                when (attempt(backend, end)) {
                    Outcome.DONE -> {
                        ended(end.sessionId)
                        return
                    }
                    Outcome.GIVE_UP -> return
                    Outcome.RETRY -> {
                        delay(retryDelaysMs[minOf(failures, retryDelaysMs.lastIndex)])
                        failures++
                    }
                }
            }
        } finally {
            val self = currentCoroutineContext()[Job]
            synchronized(jobs) {
                if (jobs[end.sessionId] === self) {
                    jobs.remove(end.sessionId)
                    _pending.update { it - end.sessionId }
                }
            }
        }
    }

    private enum class Outcome { DONE, RETRY, GIVE_UP }

    private suspend fun attempt(backend: BackendCredentials, end: PendingEnd): Outcome {
        when (val resumed = client.resumeSession(backend, end.sessionId)) {
            is BackendResult.Success -> {
                uploads.confirmReceived(end.sessionId, resumed.value.receivedCaptureIds)
                uploads.retryFailed(end.sessionId)
                flush(backend, resumed.value)
                withTimeoutOrNull(capturesTimeoutMs) {
                    uploads.all.first { !uploads.hasPending(end.sessionId) }
                }
            }
            is BackendResult.HttpError -> when (resumed.status) {
                404 -> return Outcome.DONE // no such session: nothing can be delivered any more
                409 -> Unit // ended already, or another session is active: end without flushing
                else -> return outcomeOf(resumed)
            }
            is BackendResult.Failure -> return outcomeOf(resumed)
        }
        val request = SessionEndRequest(end.clientTimeMs, end.reason, prepareNotes = true.takeIf { end.prepareNotes })
        val result = client.endSession(backend, end.sessionId, request)
        return when {
            result is BackendResult.Success -> Outcome.DONE
            result is BackendResult.HttpError && result.status in ENDED_STATUSES -> Outcome.DONE
            else -> outcomeOf(result as BackendResult.Failure)
        }
    }

    private fun outcomeOf(failure: BackendResult.Failure): Outcome =
        if (CaptureUploadQueue.isTransient(failure) && !(failure is BackendResult.HttpError && failure.status == 409)) {
            Outcome.RETRY
        } else {
            Outcome.GIVE_UP
        }

    private suspend fun flush(backend: BackendCredentials, session: Session) {
        val audio = spools.audio(session.sessionId)
        val events = spools.events(session.sessionId)
        if (!audio.hasUnacked && events.finals().isEmpty() && events.queued().isEmpty()) return
        val connection = SessionConnection(
            scope = scope,
            socketFactory = socketFactory,
            url = SessionConnection.socketUrl(backend.baseUrl, session.wsPath),
            token = backend.token,
            clock = clock,
            capabilities = SessionConnection.CAPTURE_CAPABILITIES,
            resume = { client.resumeSession(backend, session.sessionId) is BackendResult.Success },
            reconnectDelaysMs = reconnectDelaysMs,
            audio = audio,
            backlog = events,
            loopContext = loopContext,
        )
        try {
            connection.start()
            withTimeoutOrNull(drainTimeoutMs) { connection.drained.first { it } }
        } finally {
            connection.stop()
            // Wait for the socket to be let go, so a capture screen opened next on the same
            // spools (see continueInstead) never shares them with this connection.
            withContext(NonCancellable) {
                withTimeoutOrNull(STOP_TIMEOUT_MS) { connection.state.first { it == ConnectionState.Stopped } }
            }
        }
    }

    companion object {
        val DEFAULT_RETRY_DELAYS_MS: List<Long> = listOf(2_000, 5_000, 10_000, 30_000, 60_000)

        /** How long the spooled audio and events may take to go out before the session is ended anyway. */
        const val DRAIN_TIMEOUT_MS: Long = 120_000

        /** How long pending captures may take to upload before the session is ended anyway. */
        const val CAPTURES_TIMEOUT_MS: Long = 300_000

        private const val STOP_TIMEOUT_MS: Long = 5_000

        private val ENDED_STATUSES = setOf(404, 409)
    }
}
