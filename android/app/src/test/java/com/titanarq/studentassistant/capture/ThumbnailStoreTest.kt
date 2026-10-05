package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.protocol.CaptureTrigger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ThumbnailStoreTest {
    private val history = ThumbnailKey("historia", "feudalismo")
    private val physics = ThumbnailKey("fisica", "cinematica")

    private fun thumb(id: String, status: ShotStatus = ShotStatus.UPLOADED) =
        StoredThumbnail(id, "s1", id.length.toLong(), status, CaptureImageBytes(byteArrayOf(1, 2, 3)))

    @Test
    fun `add, list and remove are per topic`() {
        val store = ThumbnailStore()
        store.add(history, thumb("a"))
        store.add(history, thumb("bb"))
        store.add(physics, thumb("c"))

        assertEquals(listOf("a", "bb"), store.list(history).map { it.captureId })
        assertEquals(listOf("c"), store.list(physics).map { it.captureId })
        store.remove(history, "a")
        assertEquals(listOf("bb"), store.list(history).map { it.captureId })
        store.remove(history, "bb")
        assertEquals(emptyList<StoredThumbnail>(), store.list(history))
        assertEquals(setOf(physics), store.all.value.keys)
    }

    @Test
    fun `adding the same capture again replaces it in place`() {
        val store = ThumbnailStore()
        store.add(history, thumb("a", ShotStatus.UPLOADING))
        store.add(history, thumb("bb"))
        store.add(history, thumb("a", ShotStatus.UPLOADED))

        assertEquals(listOf("a", "bb"), store.list(history).map { it.captureId })
        assertEquals(ShotStatus.UPLOADED, store.list(history).first().status)
    }

    @Test
    fun `a topic keeps at most maxPerTopic thumbnails, dropping the oldest`() {
        val store = ThumbnailStore(maxPerTopic = 3)
        listOf("1", "2", "3", "4", "5").forEach { store.add(history, thumb(it)) }

        assertEquals(listOf("3", "4", "5"), store.list(history).map { it.captureId })
    }

    @Test
    fun `replace swaps a topic's contents and clear forgets everything`() {
        val store = ThumbnailStore(maxPerTopic = 2)
        store.add(history, thumb("a"))
        store.add(physics, thumb("p"))
        store.replace(history, listOf(thumb("x"), thumb("y"), thumb("z")))
        assertEquals(listOf("y", "z"), store.list(history).map { it.captureId })

        store.clear()
        assertEquals(emptyMap<ThumbnailKey, List<StoredThumbnail>>(), store.all.value)
    }

    @Test
    fun `only shots with a thumbnail that the camera took are kept`() {
        fun shot(status: ShotStatus, thumbnail: CaptureImageBytes?) =
            CaptureShot("a", "s1", CaptureTrigger.BUTTON, 5, status, thumbnail)
        val jpeg = CaptureImageBytes(byteArrayOf(9))

        assertNull(StoredThumbnail.of(shot(ShotStatus.CAPTURING, null)))
        assertNull(StoredThumbnail.of(shot(ShotStatus.UPLOADED, null)))
        assertNull(StoredThumbnail.of(shot(ShotStatus.CAMERA_FAILED, jpeg)))
        val kept = StoredThumbnail.of(shot(ShotStatus.PENDING, jpeg))!!
        assertEquals(ShotStatus.PENDING, kept.toShot().status)
    }
}
