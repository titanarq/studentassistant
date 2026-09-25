package com.titanarq.studentassistant.spool

import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult

/** What one [StaleSpoolSweeper.sweep] deleted. */
data class SweepResult(val sessions: List<String> = emptyList(), val captures: List<String> = emptyList())

/**
 * Deletes the spooled data of sessions that are over on the backend (#198): a session that was
 * neither continued nor ended from this phone (ended from the web, or lost by the backend) would
 * otherwise keep its audio, events and captures, and its share of the cap, for ever.
 *
 * A session's data is swept when nothing touched it for [graceMs] and its backend reports it as
 * ended or unknown: no topic of that backend lists it as its `open_session_id` (the read-only
 * `GET /api/subjects` and `GET .../topics`; resuming it just to ask would reopen it). A session
 * with a pending end (the [com.titanarq.studentassistant.capture.SessionFinisher]'s), the one
 * [activeSessionId] names, or one a capture screen opened in this process is never swept. The
 * backend asked is the one the session was bound to ([Spools.bind]) or the capture's; a session
 * with no recorded backend is swept only when every paired backend answers and none lists it. A
 * backend that is no longer paired, or that fails to answer, leaves its sessions' data alone until
 * the next sweep. Captures are swept the same way (a capture can only be uploaded into an active
 * session), so [sweep] runs before the pending captures are queued again.
 */
class StaleSpoolSweeper(
    private val spools: Spools,
    private val client: BackendClient,
    private val clock: Clock,
    private val pairedBackends: suspend () -> List<BackendCredentials>,
    private val activeSessionId: () -> String? = { null },
    private val graceMs: Long = DEFAULT_GRACE_MS,
) {
    init {
        require(graceMs >= 0) { "graceMs must not be negative" }
    }

    suspend fun sweep(): SweepResult {
        val cutoff = clock.nowMillis() - graceMs
        val ending = spools.ends().map { it.sessionId }.toSet()
        val active = activeSessionId()
        fun keep(sessionId: String) = sessionId in ending || sessionId == active
        val sessions = spools.sessionIds().filter { !keep(it) && spools.lastActivityMs(it) < cutoff }
        val captures = spools.captures.list().filter { meta ->
            !keep(meta.sessionId) && (spools.captures.storedAtMs(meta.captureId) ?: Long.MAX_VALUE) < cutoff
        }
        if (sessions.isEmpty() && captures.isEmpty()) return SweepResult()

        val paired = pairedBackends()
        val openByBackend = HashMap<String, Set<String>?>()
        suspend fun openOn(backend: BackendCredentials): Set<String>? =
            if (openByBackend.containsKey(backend.baseUrl)) {
                openByBackend[backend.baseUrl]
            } else {
                openSessions(backend).also { openByBackend[backend.baseUrl] = it }
            }

        /** True when every backend asked answered and none holds [sessionId] open. */
        suspend fun over(sessionId: String, baseUrl: String?): Boolean {
            val asked = if (baseUrl != null) paired.filter { it.baseUrl == baseUrl } else paired
            if (asked.isEmpty()) return false
            for (backend in asked) {
                val open = openOn(backend) ?: return false
                if (sessionId in open) return false
            }
            return true
        }

        val sweptSessions = sessions.filter { over(it, spools.backendOf(it)) && spools.deleteIfStale(it, cutoff) }
        val sweptCaptures = captures.filter { over(it.sessionId, it.baseUrl) }.map { meta ->
            spools.captures.remove(meta.captureId)
            meta.captureId
        }
        return SweepResult(sweptSessions, sweptCaptures)
    }

    /** The session ids [backend] holds open, or null when any list call failed. */
    private suspend fun openSessions(backend: BackendCredentials): Set<String>? {
        val subjects = when (val result = client.listSubjects(backend)) {
            is BackendResult.Success -> result.value.subjects
            is BackendResult.Failure -> return null
        }
        val open = HashSet<String>()
        for (subject in subjects) {
            when (val topics = client.listTopics(backend, subject.subjectId)) {
                is BackendResult.Success -> topics.value.topics.mapNotNullTo(open) { it.openSessionId }
                is BackendResult.Failure -> return null
            }
        }
        return open
    }

    companion object {
        /** One day: a session left for longer and over on the backend is not coming back. */
        const val DEFAULT_GRACE_MS: Long = 24L * 60 * 60 * 1000
    }
}
