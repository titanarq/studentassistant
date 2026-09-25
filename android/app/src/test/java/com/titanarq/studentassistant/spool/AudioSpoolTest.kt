package com.titanarq.studentassistant.spool

import java.io.File
import java.io.RandomAccessFile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class AudioSpoolTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val dir: File get() = File(folder.root, "audio")

    /** One frame's bytes on disk: a 20-byte record header plus 2 bytes per sample. */
    private val frameBytes = 20L + 2 * 4

    private fun frame(seq: Long) = SpooledFrame(seq, 1_000 + seq * 100, ShortArray(4) { (seq * 10 + it).toShort() })

    private fun spool(budget: SpoolBudget = SpoolBudget(1_000_000), segmentFrames: Int = 3) =
        AudioSpool(dir, budget, segmentFrames)

    @Test
    fun `frames are read back after a seq, in order, with their time and samples`() {
        val spool = spool()
        (0L..6L).forEach { spool.append(frame(it)) }

        val after = spool.after(2, 10)
        assertEquals((3L..6L).toList(), after.map { it.seq })
        assertEquals(1_300L, after.first().clientTimeMs)
        assertEquals(listOf<Short>(30, 31, 32, 33), after.first().samples.toList())
        assertEquals(listOf(3L, 4L), spool.after(2, 2).map { it.seq })
        assertEquals(6L, spool.lastSeq)
        assertTrue(spool.hasUnacked)
    }

    @Test
    fun `acknowledged frames are never read again and full segments are deleted`() {
        val budget = SpoolBudget(1_000_000)
        val spool = spool(budget)
        (0L..6L).forEach { spool.append(frame(it)) }
        assertEquals(7 * frameBytes, budget.usedBytes)

        spool.acknowledge(4)
        assertEquals(listOf(5L, 6L), spool.after(-1, 10).map { it.seq })
        assertEquals(4L, spool.ackedSeq)
        // Segment 0..2 is gone; 3..5 still holds 5, and 6 is being written.
        assertEquals(4 * frameBytes, budget.usedBytes)

        spool.acknowledge(6)
        assertFalse(spool.hasUnacked)
        assertEquals(0L, budget.usedBytes)
        assertEquals(6L, spool.lastSeq)
    }

    @Test
    fun `a reopened spool has the same frames and continues the numbering`() {
        val first = spool()
        (0L..4L).forEach { first.append(frame(it)) }
        first.acknowledge(2)
        first.close()

        val budget = SpoolBudget(1_000_000)
        val reopened = spool(budget)
        assertEquals(listOf(3L, 4L), reopened.after(-1, 10).map { it.seq })
        assertEquals(4L, reopened.lastSeq)
        assertEquals(2 * frameBytes, budget.usedBytes)

        // Everything acknowledged, then a restart: numbering still continues after 4.
        reopened.acknowledge(4)
        reopened.close()
        assertEquals(4L, spool().lastSeq)
    }

    @Test
    fun `a record cut short by a crash is dropped on reopen`() {
        val first = spool(segmentFrames = 10)
        (0L..2L).forEach { first.append(frame(it)) }
        first.close()
        val open = dir.listFiles()!!.single { it.name.endsWith(".open") }
        RandomAccessFile(open, "rw").use { it.setLength(open.length() - 5) }

        val reopened = spool(segmentFrames = 10)
        assertEquals(listOf(0L, 1L), reopened.after(-1, 10).map { it.seq })
        reopened.append(frame(2))
        assertEquals(listOf(0L, 1L, 2L), reopened.after(-1, 10).map { it.seq })
    }

    @Test
    fun `past the cap the oldest audio is dropped first, and the warning comes before`() {
        // Cap of 10 frames, warning at 80 %: segments of 3 frames.
        val budget = SpoolBudget(10 * frameBytes, warnFraction = 0.8)
        val spool = spool(budget)
        (0L..6L).forEach { spool.append(frame(it)) }
        assertFalse(budget.nearCap.value)
        spool.append(frame(7))
        assertTrue(budget.nearCap.value)

        (8L..10L).forEach { spool.append(frame(it)) } // 11 frames > cap: segment 0..2 goes
        assertEquals((3L..10L).toList(), spool.after(-1, 20).map { it.seq })
        assertEquals(3L, spool.droppedFrames)
        assertEquals(8 * frameBytes, budget.usedBytes)
        assertEquals(10L, spool.lastSeq)
    }

    @Test
    fun `captures filling the cap shrink the audio to its newest segment`() {
        val budget = SpoolBudget(10 * frameBytes)
        val spool = spool(budget)
        (0L..5L).forEach { spool.append(frame(it)) }
        budget.add(20 * frameBytes) // captures
        spool.append(frame(6))
        assertEquals(listOf(6L), spool.after(-1, 20).map { it.seq })
    }

    @Test
    fun `rebasing renumbers the held frames right after the given seq`() {
        val spool = spool()
        (0L..3L).forEach { spool.append(frame(it)) }
        spool.acknowledge(0)
        spool.rebaseAfter(41)

        val frames = spool.after(-1, 10)
        assertEquals(listOf(42L, 43L, 44L), frames.map { it.seq })
        assertEquals(listOf(1_100L, 1_200L, 1_300L), frames.map { it.clientTimeMs })
        assertEquals(44L, spool.lastSeq)
        spool.close()
        assertEquals(listOf(42L, 43L, 44L), spool().after(-1, 10).map { it.seq })
    }

    @Test
    fun `the memory backlog keeps the newest frames up to its size`() {
        val memory = MemoryAudioBacklog(maxFrames = 2)
        (0L..3L).forEach { memory.append(frame(it)) }
        assertEquals(listOf(2L, 3L), memory.after(-1, 10).map { it.seq })
        memory.acknowledge(2)
        assertEquals(listOf(3L), memory.after(-1, 10).map { it.seq })
        memory.rebaseAfter(9)
        assertEquals(listOf(10L), memory.after(-1, 10).map { it.seq })
        assertEquals(10L, memory.lastSeq)
    }
}
