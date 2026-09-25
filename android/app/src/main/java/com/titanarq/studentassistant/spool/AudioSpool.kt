package com.titanarq.studentassistant.spool

import java.io.DataInputStream
import java.io.EOFException
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** One 100 ms frame of PCM16 audio with its per-session `seq` and the client time of its first sample. */
class SpooledFrame(val seq: Long, val clientTimeMs: Long, val samples: ShortArray) {
    override fun toString(): String = "SpooledFrame(seq=$seq, clientTimeMs=$clientTimeMs, ${samples.size} samples)"
}

/**
 * The audio frames of one session that the backend has not acknowledged yet (server STT mode), in
 * `seq` order. [SessionConnection][com.titanarq.studentassistant.capture.SessionConnection] appends
 * every frame it produces, resends [after] the last acknowledged `seq` and [acknowledge]s what the
 * server `ack` covers. Implementations are thread-safe.
 */
interface AudioBacklog {
    /** The highest `seq` ever appended, acknowledged or dropped; -1 before any. The next frame is `lastSeq + 1`. */
    val lastSeq: Long

    /** The highest `seq` the backend acknowledged as far as this backlog knows; -1 when unknown. */
    val ackedSeq: Long

    /** Bytes of audio held. */
    val bytes: Long

    /** True while some held frame is above [ackedSeq]. */
    val hasUnacked: Boolean

    fun append(frame: SpooledFrame)

    /** Up to [limit] held frames with a `seq` above both [seq] and [ackedSeq], ascending. */
    fun after(seq: Long, limit: Int): List<SpooledFrame>

    /** The backend holds every frame up to [seq]: they are deleted. */
    fun acknowledge(seq: Long)

    /**
     * The backend acknowledges [seq], at or past [lastSeq] (a resumed session after this backlog
     * lost its numbering): the held frames are renumbered `seq + 1, seq + 2, ...` in order.
     */
    fun rebaseAfter(seq: Long)
}

/** An in-memory [AudioBacklog] of at most [maxFrames] frames (the oldest are dropped). */
class MemoryAudioBacklog(private val maxFrames: Int) : AudioBacklog {
    private val frames = ArrayDeque<SpooledFrame>()
    private var last = -1L
    private var acked = -1L

    override val lastSeq: Long get() = synchronized(this) { last }
    override val ackedSeq: Long get() = synchronized(this) { acked }
    override val bytes: Long get() = synchronized(this) { frames.sumOf { it.samples.size * 2L } }
    override val hasUnacked: Boolean get() = synchronized(this) { frames.any { it.seq > acked } }

    override fun append(frame: SpooledFrame): Unit = synchronized(this) {
        frames.addLast(frame)
        last = maxOf(last, frame.seq)
        while (frames.size > maxFrames) frames.removeFirst()
    }

    override fun after(seq: Long, limit: Int): List<SpooledFrame> = synchronized(this) {
        val from = maxOf(seq, acked)
        frames.asSequence().filter { it.seq > from }.take(limit).toList()
    }

    override fun acknowledge(seq: Long): Unit = synchronized(this) {
        acked = maxOf(acked, seq)
        last = maxOf(last, seq)
        while (frames.isNotEmpty() && frames.first().seq <= acked) frames.removeFirst()
    }

    override fun rebaseAfter(seq: Long): Unit = synchronized(this) {
        val renumbered = frames.mapIndexed { index, frame -> SpooledFrame(seq + 1 + index, frame.clientTimeMs, frame.samples) }
        frames.clear()
        frames.addAll(renumbered)
        acked = seq
        last = maxOf(seq, renumbered.lastOrNull()?.seq ?: seq)
    }
}

/**
 * A file-backed [AudioBacklog] in [dir] (app-private storage), so unacknowledged audio survives the
 * app process dying.
 *
 * Frames go into segment files of [segmentFrames] frames: `seg-<first seq>.open` while being
 * written (each frame is written through as it arrives), renamed `seg-<first>-<last>.pcm` when
 * full. A record is `seq` (i64), client time (i64), sample count (i32), then the PCM16 samples
 * little-endian; a record cut short by a crash is truncated away on the next open. A segment is
 * deleted once every frame in it is acknowledged, and `floor` keeps the highest `seq` ever deleted
 * so numbering continues after a restart with an empty spool.
 *
 * Every byte is reported to [budget]; after an append that leaves the budget over its cap, the
 * oldest segments are deleted (acknowledged or not) until it fits or only the segment being
 * written is left.
 */
class AudioSpool(
    private val dir: File,
    private val budget: SpoolBudget,
    private val segmentFrames: Int = DEFAULT_SEGMENT_FRAMES,
) : AudioBacklog {
    private class Segment(val first: Long, var last: Long, var file: File, var bytes: Long, var open: Boolean)

    private val segments = ArrayDeque<Segment>()
    private var floor = -1L
    private var acked = -1L
    private var out: FileOutputStream? = null
    private var openCount = 0
    private val openFrames = mutableListOf<SpooledFrame>()
    private var total = 0L

    /** Frames the budget dropped unacknowledged since this spool was opened. */
    var droppedFrames: Long = 0
        private set

    init {
        require(segmentFrames > 0) { "segmentFrames must be positive" }
        dir.mkdirs()
        load()
    }

    override val lastSeq: Long get() = synchronized(this) { maxOf(floor, acked, segments.lastOrNull()?.last ?: -1L) }
    override val ackedSeq: Long get() = synchronized(this) { acked }
    override val bytes: Long get() = synchronized(this) { total }
    override val hasUnacked: Boolean get() = synchronized(this) { segments.any { it.last > acked } }

    override fun append(frame: SpooledFrame): Unit = synchronized(this) {
        if (frame.seq <= lastSeq) return
        val record = encode(frame)
        val segment = segments.lastOrNull()?.takeIf { it.open } ?: startSegment(frame.seq)
        try {
            val stream = out ?: FileOutputStream(segment.file, true).also { out = it }
            stream.write(record)
        } catch (e: IOException) {
            // A full disk: this frame is not kept. The live send still carries it.
            return
        }
        segment.last = frame.seq
        segment.bytes += record.size
        grow(record.size.toLong())
        openFrames += frame
        openCount++
        if (openCount >= segmentFrames) closeSegment(segment)
        evictOverCap()
    }

    override fun after(seq: Long, limit: Int): List<SpooledFrame> = synchronized(this) {
        val from = maxOf(seq, acked)
        val result = mutableListOf<SpooledFrame>()
        for (segment in segments) {
            if (result.size >= limit) break
            if (segment.last <= from) continue
            val frames = if (segment.open) openFrames.toList() else read(segment.file)
            for (frame in frames) {
                if (frame.seq > from) result += frame
                if (result.size >= limit) break
            }
        }
        result
    }

    override fun acknowledge(seq: Long): Unit = synchronized(this) {
        if (seq <= acked) return
        acked = seq
        while (segments.isNotEmpty() && segments.first().last <= seq) deleteFirst()
    }

    override fun rebaseAfter(seq: Long): Unit = synchronized(this) {
        val frames = after(-1, Int.MAX_VALUE)
        while (segments.isNotEmpty()) deleteFirst()
        floor = maxOf(floor, seq)
        writeFloor()
        acked = seq
        frames.forEachIndexed { index, frame -> append(SpooledFrame(seq + 1 + index, frame.clientTimeMs, frame.samples)) }
    }

    /** Closes the segment being written; the spool stays usable (a later append reopens a segment). */
    fun close(): Unit = synchronized(this) {
        out?.close()
        out = null
    }

    private fun startSegment(first: Long): Segment {
        val segment = Segment(first, first - 1, File(dir, "seg-${pad(first)}$OPEN_SUFFIX"), 0, open = true)
        segments.addLast(segment)
        openCount = 0
        openFrames.clear()
        return segment
    }

    private fun closeSegment(segment: Segment) {
        out?.close()
        out = null
        val closed = File(dir, "seg-${pad(segment.first)}-${pad(segment.last)}$CLOSED_SUFFIX")
        if (segment.file.renameTo(closed)) segment.file = closed
        segment.open = false
        openCount = 0
        openFrames.clear()
    }

    private fun deleteFirst() {
        val segment = segments.removeFirst()
        if (segment.open) {
            out?.close()
            out = null
            openCount = 0
            openFrames.clear()
        }
        segment.file.delete()
        grow(-segment.bytes)
        if (segment.last > floor) {
            floor = segment.last
            writeFloor()
        }
    }

    private fun evictOverCap() {
        while (budget.overCap && segments.size > 1) {
            val oldest = segments.first()
            droppedFrames += countUnacked(oldest)
            deleteFirst()
        }
    }

    private fun countUnacked(segment: Segment): Long =
        if (segment.last <= acked) 0 else segment.last - maxOf(segment.first - 1, acked)

    private fun grow(delta: Long) {
        total += delta
        budget.add(delta)
    }

    private fun load() {
        floor = File(dir, FLOOR_FILE).takeIf { it.isFile }?.readText()?.trim()?.toLongOrNull() ?: -1L
        val files = dir.listFiles().orEmpty().filter { it.name.startsWith("seg-") }.sortedBy { it.name }
        for (file in files) {
            val frames = read(file, truncate = true)
            if (frames.isEmpty() || frames.last().seq <= floor) {
                file.delete()
                continue
            }
            var target = file
            if (file.name.endsWith(OPEN_SUFFIX)) {
                // A segment interrupted by a crash: close it as it is.
                val closed = File(dir, "seg-${pad(frames.first().seq)}-${pad(frames.last().seq)}$CLOSED_SUFFIX")
                if (file.renameTo(closed)) target = closed
            }
            val segment = Segment(frames.first().seq, frames.last().seq, target, target.length(), open = false)
            segments.addLast(segment)
            grow(segment.bytes)
        }
    }

    private fun writeFloor() {
        val file = File(dir, FLOOR_FILE)
        val temp = File(dir, "$FLOOR_FILE.tmp")
        try {
            temp.writeText(floor.toString())
            if (!temp.renameTo(file)) file.writeText(floor.toString())
        } catch (e: IOException) {
            // Numbering after a restart falls back to the frames still on disk.
        }
    }

    private fun read(file: File, truncate: Boolean = false): List<SpooledFrame> {
        val frames = mutableListOf<SpooledFrame>()
        var good = 0L
        try {
            DataInputStream(file.inputStream().buffered()).use { input ->
                while (true) {
                    val seq = try {
                        input.readLong()
                    } catch (e: EOFException) {
                        break
                    }
                    val time = input.readLong()
                    val count = input.readInt()
                    if (count < 0 || count > MAX_SAMPLES) throw EOFException("corrupt record")
                    val raw = ByteArray(count * 2)
                    input.readFully(raw)
                    val samples = ShortArray(count)
                    ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(samples)
                    frames += SpooledFrame(seq, time, samples)
                    good += RECORD_HEADER + raw.size
                }
            }
        } catch (e: IOException) {
            // EOF inside a record (a crash mid-write) or a corrupt one: keep what came before.
            if (truncate) RandomAccessFile(file, "rw").use { it.setLength(good) }
        }
        return frames
    }

    private fun encode(frame: SpooledFrame): ByteArray {
        val buffer = ByteBuffer.allocate(RECORD_HEADER + frame.samples.size * 2)
        buffer.putLong(frame.seq)
        buffer.putLong(frame.clientTimeMs)
        buffer.putInt(frame.samples.size)
        buffer.order(ByteOrder.LITTLE_ENDIAN)
        for (sample in frame.samples) buffer.putShort(sample)
        return buffer.array()
    }

    companion object {
        /** 50 frames of 100 ms: 5 s, ~160 KB per segment. */
        const val DEFAULT_SEGMENT_FRAMES: Int = 50

        private const val RECORD_HEADER = 20
        private const val MAX_SAMPLES = 1 shl 20
        private const val OPEN_SUFFIX = ".open"
        private const val CLOSED_SUFFIX = ".pcm"
        private const val FLOOR_FILE = "floor"

        private fun pad(seq: Long): String = seq.toString().padStart(20, '0')
    }
}
