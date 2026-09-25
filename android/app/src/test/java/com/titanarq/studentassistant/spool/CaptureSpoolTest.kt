package com.titanarq.studentassistant.spool

import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.Button
import com.titanarq.studentassistant.protocol.ButtonName
import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.Marker
import com.titanarq.studentassistant.protocol.SessionEndReason
import com.titanarq.studentassistant.protocol.TranscriptClientFinal
import java.io.File
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class CaptureSpoolTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val dir: File get() = File(folder.root, "captures")

    private fun meta(id: String, time: Long, images: Int = 2) = SpooledCaptureMeta(
        sessionId = "s1",
        baseUrl = "http://pc:8000",
        request = CaptureUploadRequest(
            captureId = id,
            trigger = CaptureTrigger.COMMAND,
            commandId = "cmd-1",
            clientTimeMs = time,
            images = (0 until images).map { CaptureImage("image_$it", "image/jpeg", 3000, 4000, time + it) },
        ),
    )

    private fun images(vararg first: Byte) = first.map { CaptureImageBytes(byteArrayOf(it, it, it)) }

    @Test
    fun `a stored burst survives a new spool over the same directory`() {
        val budget = SpoolBudget(1_000_000)
        val spool = CaptureSpool(dir, budget)
        assertTrue(spool.put(meta("c-2", 2_000), images(3, 4), CaptureImageBytes(byteArrayOf(9))))
        assertTrue(spool.put(meta("c-1", 1_000), images(1, 2), null))
        assertEquals(6 + 6 + 1 + 0L, budget.usedBytes - metaBytes())

        val reopenedBudget = SpoolBudget(1_000_000)
        val reopened = CaptureSpool(dir, reopenedBudget)
        assertEquals(budget.usedBytes, reopenedBudget.usedBytes)
        val listed = reopened.list()
        assertEquals(listOf("c-1", "c-2"), listed.map { it.captureId })
        assertEquals(meta("c-2", 2_000).copy(hasThumbnail = true), listed[1])
        assertArrayEquals(byteArrayOf(3, 3, 3), reopened.images("c-2")!![0].bytes)
        assertArrayEquals(byteArrayOf(4, 4, 4), reopened.images("c-2")!![1].bytes)
        assertArrayEquals(byteArrayOf(9), reopened.thumbnail("c-2")!!.bytes)
        assertNull(reopened.thumbnail("c-1"))
    }

    private fun metaBytes(): Long = dir.walkBottomUp().filter { it.name == "meta.json" }.sumOf { it.length() }

    @Test
    fun `remove deletes the files and gives the bytes back`() {
        val budget = SpoolBudget(1_000_000)
        val spool = CaptureSpool(dir, budget)
        spool.put(meta("c-1", 1_000), images(1, 2), null)
        assertTrue(spool.contains("c-1"))
        spool.remove("c-1")
        assertFalse(spool.contains("c-1"))
        assertNull(spool.images("c-1"))
        assertEquals(emptyList<SpooledCaptureMeta>(), spool.list())
        assertEquals(0L, budget.usedBytes)
    }

    @Test
    fun `a burst interrupted before its metadata is discarded`() {
        File(dir, "c-9").mkdirs()
        File(dir, "c-9/image_0").writeBytes(byteArrayOf(1))
        val budget = SpoolBudget(1_000_000)
        val spool = CaptureSpool(dir, budget)
        assertEquals(emptyList<SpooledCaptureMeta>(), spool.list())
        assertFalse(File(dir, "c-9").exists())
        assertEquals(0L, budget.usedBytes)
    }

    @Test
    fun `captures are kept past the cap and raise the warning`() {
        val budget = SpoolBudget(10)
        val spool = CaptureSpool(dir, budget)
        assertTrue(spool.put(meta("c-1", 1_000), images(1, 2), null))
        assertTrue(budget.overCap)
        assertTrue(budget.nearCap.value)
        assertEquals(1, spool.list().size)
    }

    @Test
    fun `an id that is not a safe file name is refused`() {
        val spool = CaptureSpool(dir, SpoolBudget(1_000_000))
        assertFalse(spool.put(meta("../x", 1_000), images(1, 2), null))
        assertNull(spool.images("../x"))
    }

    @Test
    fun `the event spool keeps finals and queued events across instances`() {
        val file = File(folder.root, "events.json")
        val first = EventSpool(file)
        val finalA = TranscriptClientFinal("a-0", 1, 2, "hola", "android-speech", "es-ES")
        val finalB = TranscriptClientFinal("a-1", 3, 4, "adiós", "android-speech", "es-ES", 0.9)
        first.putFinal(finalA)
        first.putFinal(finalB)
        first.enqueue(Button(ButtonName.IMPORTANT, null, 5))
        first.enqueue(Marker(6, "ojo"))
        first.confirmFinals(listOf("a-0"))
        first.dequeue(1)

        val reopened = EventSpool(file)
        assertEquals(listOf(finalB), reopened.finals())
        assertEquals(listOf(Marker(6, "ojo")), reopened.queued())

        reopened.confirmFinals(listOf("a-1"))
        reopened.dequeue(1)
        assertFalse(file.exists())
        assertEquals(emptyList<TranscriptClientFinal>(), EventSpool(file).finals())
    }

    @Test
    fun `an unreadable event spool starts over`() {
        val file = File(folder.root, "events.json")
        file.writeText("{not json")
        val spool = EventSpool(file)
        assertEquals(emptyList<TranscriptClientFinal>(), spool.finals())
        spool.enqueue(Marker(1))
        assertEquals(listOf(Marker(1)), EventSpool(file).queued())
    }

    @Test
    fun `pending ends and session spools live under one root`() {
        val spools = Spools(File(folder.root, "spool"), SpoolBudget(1_000_000))
        val end = PendingEnd("s1", "http://pc:8000", 9_000, SessionEndReason.BUTTON)
        spools.putEnd(end)
        spools.audio("s1").append(SpooledFrame(0, 1, shortArrayOf(1)))
        spools.events("s1").enqueue(Marker(1))

        val reopened = Spools(File(folder.root, "spool"), SpoolBudget(1_000_000))
        assertEquals(listOf(end), reopened.ends())
        assertEquals(listOf("s1"), reopened.sessionIds())
        assertTrue(reopened.budget.usedBytes > 0)
        assertEquals(listOf(Marker(1)), reopened.events("s1").queued())

        reopened.deleteSession("s1")
        reopened.removeEnd("s1")
        assertEquals(emptyList<PendingEnd>(), reopened.ends())
        assertEquals(emptyList<String>(), reopened.sessionIds())
        assertEquals(0L, reopened.budget.usedBytes)
    }
}
