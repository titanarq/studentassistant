package com.titanarq.studentassistant.spool

import com.titanarq.studentassistant.protocol.ClientEvent
import com.titanarq.studentassistant.protocol.ProtocolJson
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import java.io.File
import java.io.IOException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.Serializable

/**
 * The text messages of one session the backend may not have yet: transcript finals until the
 * backend confirms them, and the `button` / `marker` / `ack` events produced while disconnected,
 * in order. Implementations are thread-safe.
 */
interface EventBacklog {
    /** Unconfirmed finals, oldest first. */
    fun finals(): List<TranscriptClientFinal>

    /** Adds (or replaces, same `segment_id`) an unconfirmed final; the oldest go past the cap. */
    fun putFinal(final: TranscriptClientFinal)

    /** The backend has these finals. */
    fun confirmFinals(segmentIds: Collection<String>)

    /** Events waiting for a connection, oldest first. */
    fun queued(): List<ClientEvent>

    /** Queues [event]; the oldest go past the cap. */
    fun enqueue(event: ClientEvent)

    /** The first [count] queued events were sent. */
    fun dequeue(count: Int)
}

/** An in-memory [EventBacklog]. */
class MemoryEventBacklog(
    private val maxFinals: Int = DEFAULT_MAX_FINALS,
    private val maxQueued: Int = DEFAULT_MAX_QUEUED,
) : EventBacklog {
    private val finals = LinkedHashMap<String, TranscriptClientFinal>()
    private val queue = ArrayDeque<ClientEvent>()

    override fun finals(): List<TranscriptClientFinal> = synchronized(this) { finals.values.toList() }

    override fun putFinal(final: TranscriptClientFinal): Unit = synchronized(this) {
        finals[final.segmentId] = final
        while (finals.size > maxFinals) finals.remove(finals.keys.first())
    }

    override fun confirmFinals(segmentIds: Collection<String>): Unit = synchronized(this) {
        segmentIds.forEach(finals::remove)
    }

    override fun queued(): List<ClientEvent> = synchronized(this) { queue.toList() }

    override fun enqueue(event: ClientEvent): Unit = synchronized(this) {
        queue.addLast(event)
        while (queue.size > maxQueued) queue.removeFirst()
    }

    override fun dequeue(count: Int): Unit = synchronized(this) {
        repeat(minOf(count, queue.size)) { queue.removeFirst() }
    }

    companion object {
        const val DEFAULT_MAX_FINALS: Int = 200
        const val DEFAULT_MAX_QUEUED: Int = 100
    }
}

/**
 * A file-backed [EventBacklog]: [file] (app-private storage) holds the whole backlog as JSON and is
 * rewritten atomically (a temporary file, then a rename) on every change, so it survives the app
 * process dying. It stays small: finals leave it as soon as the backend echoes them. A file that
 * cannot be read is started over.
 */
class EventSpool(
    private val file: File,
    private val maxFinals: Int = MemoryEventBacklog.DEFAULT_MAX_FINALS,
    private val maxQueued: Int = MemoryEventBacklog.DEFAULT_MAX_QUEUED,
) : EventBacklog {
    @Serializable
    private data class Stored(val finals: List<TranscriptClientFinal> = emptyList(), val queued: List<ClientEvent> = emptyList())

    private val memory = MemoryEventBacklog(maxFinals, maxQueued)

    init {
        file.parentFile?.mkdirs()
        val stored = try {
            if (file.isFile) ProtocolJson.decodeFromString(Stored.serializer(), file.readText()) else null
        } catch (e: SerializationException) {
            null
        } catch (e: IllegalArgumentException) {
            null
        } catch (e: IOException) {
            null
        }
        stored?.finals?.forEach(memory::putFinal)
        stored?.queued?.forEach(memory::enqueue)
    }

    override fun finals(): List<TranscriptClientFinal> = memory.finals()

    override fun putFinal(final: TranscriptClientFinal): Unit = synchronized(this) {
        memory.putFinal(final)
        save()
    }

    override fun confirmFinals(segmentIds: Collection<String>): Unit = synchronized(this) {
        val before = memory.finals().size
        memory.confirmFinals(segmentIds)
        if (memory.finals().size != before) save()
    }

    override fun queued(): List<ClientEvent> = memory.queued()

    override fun enqueue(event: ClientEvent): Unit = synchronized(this) {
        memory.enqueue(event)
        save()
    }

    override fun dequeue(count: Int): Unit = synchronized(this) {
        if (count <= 0) return
        memory.dequeue(count)
        save()
    }

    private fun save() {
        val finals = memory.finals()
        val queued = memory.queued()
        try {
            if (finals.isEmpty() && queued.isEmpty()) {
                file.delete()
                return
            }
            val temp = File(file.path + ".tmp")
            temp.writeText(ProtocolJson.encodeToString(Stored.serializer(), Stored(finals, queued)))
            if (!temp.renameTo(file)) file.writeText(temp.readText())
        } catch (e: IOException) {
            // A full disk: the backlog stays in memory for this process.
        }
    }
}
