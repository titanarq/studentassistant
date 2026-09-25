package com.titanarq.studentassistant.spool

import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** The cap across sessions and the stale-session guard of [Spools] (#198). */
class SpoolsTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val root: File get() = File(folder.root, "spool")

    /** One frame's bytes on disk: a 20-byte record header plus 2 bytes per sample. */
    private val frameBytes = 20L + 2 * 4

    private fun frame(seq: Long, time: Long) = SpooledFrame(seq, time, ShortArray(4))

    /** Appends [count] frames from `seq` 0, one segment ([SEGMENT] frames) at a time. */
    private fun AudioSpool.fill(count: Int, startMs: Long) = (0 until count).forEach { append(frame(it.toLong(), startMs + it * 100L)) }

    private fun spools(maxBytes: Long) = Spools(root, SpoolBudget(maxBytes))

    private fun capture(id: String, bytes: Int) = SpooledCaptureMeta(
        sessionId = "s-new",
        baseUrl = "http://pc:8000",
        request = CaptureUploadRequest(id, CaptureTrigger.BUTTON, null, 1, listOf(CaptureImage("image_0", "image/jpeg", 1, 1, 1))),
    ) to listOf(CaptureImageBytes(ByteArray(bytes)))

    @Test
    fun `past the cap the oldest audio of any session goes first, never the segment being written`() {
        val segment = AudioSpool.DEFAULT_SEGMENT_FRAMES
        val segmentBytes = segment * frameBytes
        // Room for four full segments and a bit.
        val spools = spools(4 * segmentBytes + 10)
        val old = spools.audio("s-old")
        val mid = spools.audio("s-mid")
        old.fill(2 * segment, startMs = 1_000) // two closed segments, the oldest audio
        mid.fill(segment, startMs = 50_000) // one closed segment
        val live = spools.audio("s-live")
        live.fill(segment + 3, startMs = 90_000) // one closed segment, one being written: over the cap

        // The oldest segment of s-old went, nothing else.
        assertEquals((segment.toLong() until 2L * segment).toList(), old.after(-1, Int.MAX_VALUE).map { it.seq })
        assertEquals(segment, mid.after(-1, Int.MAX_VALUE).size)
        assertEquals(segment + 3, live.after(-1, Int.MAX_VALUE).size)
        assertEquals(segment.toLong(), old.droppedFrames)
        assertFalse(spools.budget.overCap)

        // Much more live audio: every other session's audio goes before the live session's own.
        (segment + 3 until 3 * segment + 3).forEach { live.append(frame(it.toLong(), 90_000 + it * 100L)) }
        assertEquals(emptyList<SpooledFrame>(), old.after(-1, Int.MAX_VALUE))
        assertEquals(emptyList<SpooledFrame>(), mid.after(-1, Int.MAX_VALUE))
        assertEquals(3 * segment + 3, live.after(-1, Int.MAX_VALUE).size)
        assertTrue(spools.budget.usedBytes <= spools.budget.maxBytes)
    }

    @Test
    fun `captures are never dropped, they push the audio out, down to the segment being written`() {
        val segment = AudioSpool.DEFAULT_SEGMENT_FRAMES
        val spools = spools(3 * segment * frameBytes)
        val other = spools.audio("s-other")
        other.fill(segment, startMs = 1_000)
        val live = spools.audio("s-live")
        live.fill(segment + 2, startMs = 5_000)

        val (meta, images) = capture("c-1", bytes = (3 * segment * frameBytes).toInt())
        assertTrue(spools.captures.put(meta, images, null))

        assertTrue(spools.captures.contains("c-1"))
        assertEquals(emptyList<SpooledFrame>(), other.after(-1, Int.MAX_VALUE))
        // Only the frames of the segment being written are left.
        assertEquals(listOf(segment.toLong(), segment + 1L), live.after(-1, Int.MAX_VALUE).map { it.seq })
        assertTrue(spools.budget.overCap) // the photos alone pass it; the warning stays on
    }

    @Test
    fun `audio already on disk over a lowered cap is trimmed oldest first at the next start`() {
        val segment = AudioSpool.DEFAULT_SEGMENT_FRAMES
        spools(1_000_000).also {
            it.audio("s-a").fill(segment, startMs = 2_000)
            it.audio("s-b").fill(segment, startMs = 1_000)
            it.audio("s-a").close()
            it.audio("s-b").close()
        }
        val reopened = spools(segment * frameBytes + 10)
        assertEquals(segment, reopened.audio("s-a").after(-1, Int.MAX_VALUE).size)
        assertEquals(emptyList<SpooledFrame>(), reopened.audio("s-b").after(-1, Int.MAX_VALUE))
    }

    @Test
    fun `a session is only deleted as stale when untouched, not bound here and with no pending end`() {
        val spools = spools(1_000_000)
        spools.audio("s1").append(frame(0, 1))
        spools.events("s2").putFinal(com.titanarq.studentassistant.protocol.TranscriptClientFinal("a", 1, 2, "x", "p", "es-ES"))
        spools.audio("s3").append(frame(0, 1))
        spools.putEnd(PendingEnd("s3", "http://pc:8000", 1))
        val now = System.currentTimeMillis()

        assertFalse(spools.deleteIfStale("s1", cutoffMs = now - 60_000)) // touched a moment ago
        assertTrue(spools.deleteIfStale("s1", cutoffMs = now + 60_000))
        assertFalse(spools.deleteIfStale("s3", cutoffMs = now + 60_000)) // its end is pending
        spools.bind("s2", "http://pc:8000")
        assertEquals("http://pc:8000", spools.backendOf("s2"))
        assertFalse(spools.deleteIfStale("s2", cutoffMs = now + 60_000)) // a capture screen has it

        assertEquals(listOf("s2", "s3"), spools.sessionIds().sorted())
        assertEquals("http://pc:8000", spools(1_000_000).backendOf("s2")) // recorded on disk
    }
}
