package com.titanarq.studentassistant.spool

import com.titanarq.studentassistant.protocol.ProtocolJson
import com.titanarq.studentassistant.protocol.SessionEndReason
import java.io.File
import java.io.IOException
import kotlinx.serialization.SerialName
import kotlinx.serialization.SerializationException
import kotlinx.serialization.Serializable

/** A session the student ended that the backend has not been told about yet. */
@Serializable
data class PendingEnd(
    @SerialName("session_id") val sessionId: String,
    @SerialName("base_url") val baseUrl: String,
    /** Phone time of "Terminar", sent as the end request's `client_time_ms`. */
    @SerialName("client_time_ms") val clientTimeMs: Long,
    val reason: SessionEndReason = SessionEndReason.BUTTON,
)

/**
 * Every spool of the app under one root in app-private storage (`filesDir/spool`):
 *
 * - `sessions/<session_id>/audio/`: that session's [AudioSpool];
 * - `sessions/<session_id>/events.json`: its [EventSpool] (unconfirmed finals, queued events);
 * - `captures/<capture_id>/`: the [CaptureSpool] of pending bursts (all sessions);
 * - `ends/<session_id>.json`: the [PendingEnd]s.
 *
 * One [budget] caps them all. Each session's spools are created once per process and shared, so
 * the capture screen and the [com.titanarq.studentassistant.capture.SessionFinisher] never write
 * the same files through two objects. Thread-safe.
 */
class Spools(val root: File, val budget: SpoolBudget) {
    private val audio = HashMap<String, AudioSpool>()
    private val events = HashMap<String, EventSpool>()
    private val sessionsDir = File(root, "sessions")
    private val endsDir = File(root, "ends")

    /** The pending capture bursts. */
    val captures: CaptureSpool = CaptureSpool(File(root, "captures"), budget)

    init {
        // Count the audio already on disk in the budget from the start.
        synchronized(this) { sessionIds().forEach(::audio) }
    }

    fun audio(sessionId: String): AudioSpool = synchronized(this) {
        audio.getOrPut(sessionId) { AudioSpool(File(sessionDir(sessionId), "audio"), budget) }
    }

    fun events(sessionId: String): EventSpool = synchronized(this) {
        events.getOrPut(sessionId) { EventSpool(File(sessionDir(sessionId), "events.json")) }
    }

    /** Session ids with spooled audio or events on disk. */
    fun sessionIds(): List<String> = synchronized(this) {
        sessionsDir.listFiles().orEmpty().filter { it.isDirectory && SAFE_ID.matches(it.name) }.map { it.name }
    }

    /** The session is over on the backend: its audio and events are deleted. */
    fun deleteSession(sessionId: String): Unit = synchronized(this) {
        audio.remove(sessionId)?.let { spool ->
            spool.close()
            spool.acknowledge(Long.MAX_VALUE - 1)
        }
        events.remove(sessionId)
        sessionDir(sessionId).deleteRecursively()
    }

    fun putEnd(end: PendingEnd): Unit = synchronized(this) {
        val file = endFile(end.sessionId)
        try {
            endsDir.mkdirs()
            val temp = File(file.path + ".tmp")
            temp.writeText(ProtocolJson.encodeToString(PendingEnd.serializer(), end))
            if (!temp.renameTo(file)) file.writeText(temp.readText())
        } catch (e: IOException) {
            // Kept in memory by the finisher for this process.
        }
    }

    fun ends(): List<PendingEnd> = synchronized(this) {
        endsDir.listFiles().orEmpty().filter { it.name.endsWith(".json") }.mapNotNull { file ->
            try {
                ProtocolJson.decodeFromString(PendingEnd.serializer(), file.readText())
            } catch (e: IOException) {
                null
            } catch (e: SerializationException) {
                null
            } catch (e: IllegalArgumentException) {
                null
            }
        }
    }

    fun removeEnd(sessionId: String): Unit = synchronized(this) {
        endFile(sessionId).delete()
    }

    private fun sessionDir(sessionId: String): File {
        require(SAFE_ID.matches(sessionId)) { "not a session id: $sessionId" }
        return File(sessionsDir, sessionId)
    }

    private fun endFile(sessionId: String): File {
        require(SAFE_ID.matches(sessionId)) { "not a session id: $sessionId" }
        return File(endsDir, "$sessionId.json")
    }

    companion object {
        const val DIR_NAME: String = "spool"
        private val SAFE_ID = Regex("[A-Za-z0-9_-]{1,128}")
    }
}
