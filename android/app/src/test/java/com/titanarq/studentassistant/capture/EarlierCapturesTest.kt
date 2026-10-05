package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.backend.TopicCapture
import com.titanarq.studentassistant.backend.TopicCapturesResponse
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class EarlierCapturesTest {
    private val key = ThumbnailKey("historia", "feudalismo")
    private val credentials = BackendCredentials("http://pc:8000", "tok", "laura")
    private val client = FakeBackendClient()
    private val store = ThumbnailStore()
    private val loader = EarlierCaptures(client, store)

    private fun listing(vararg ids: Pair<String, Long>) = BackendResult.Success(
        TopicCapturesResponse(
            "historia",
            "feudalismo",
            ids.map { (id, at) -> TopicCapture(id, "s0", at, "/api/subjects/historia/topics/feudalismo/captures/$id/thumbnail") },
        ),
    )

    @Test
    fun `the topic's earlier photos fill the store as uploaded, oldest first`() = runTest {
        client.listTopicCapturesResult = listing("a" to 10L, "b" to 20L)
        client.captureThumbnailResult = BackendResult.Success(byteArrayOf(1, 2))

        loader.load(credentials, key)

        val stored = store.list(key)
        assertEquals(listOf("a", "b"), stored.map { it.captureId })
        assertTrue(stored.all { it.status == ShotStatus.UPLOADED && it.sessionId == "s0" })
        assertEquals(listOf(10L, 20L), stored.map { it.clientTimeMs })
        assertEquals("listTopicCaptures http://pc:8000 historia/feudalismo", client.calls.first())
    }

    @Test
    fun `earlier photos arriving after this session's own ones still sort before them`() = runTest {
        store.add(key, StoredThumbnail("now", "s1", 100, ShotStatus.UPLOADING, CaptureImageBytes(byteArrayOf(9))))
        client.listTopicCapturesResult = listing("old" to 10L, "now" to 100L)
        client.captureThumbnailResult = BackendResult.Success(byteArrayOf(1))

        loader.load(credentials, key)

        assertEquals(listOf("old", "now"), store.list(key).map { it.captureId })
        // The local photo keeps its own status and bytes and is not downloaded again.
        assertEquals(ShotStatus.UPLOADING, store.list(key).last().status)
        assertEquals(1, client.calls.count { it.startsWith("captureThumbnail") })
    }

    @Test
    fun `a failed listing or thumbnail leaves the store as it was`() = runTest {
        client.listTopicCapturesResult = BackendResult.Unreachable("offline")
        loader.load(credentials, key)
        assertEquals(emptyList<StoredThumbnail>(), store.list(key))

        client.listTopicCapturesResult = listing("a" to 1L, "b" to 2L)
        client.captureThumbnailResult = BackendResult.HttpError(404)
        loader.load(credentials, key)
        assertEquals(emptyList<StoredThumbnail>(), store.list(key))
    }
}
