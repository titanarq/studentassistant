package com.titanarq.studentassistant.backend

import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

class OkHttpTopicCapturesTest {
    private val server = MockWebServer()
    private val client = OkHttpBackendClient()
    private lateinit var backend: BackendCredentials

    @Before
    fun setUp() {
        server.start()
        backend = BackendCredentials(server.url("/").toString().trimEnd('/'), "sa_tok", "laura")
    }

    @After
    fun tearDown() = server.shutdown()

    @Test
    fun `the listing is a GET of the topic's captures as the active user`() = runTest {
        server.enqueue(
            MockResponse().setHeader("Content-Type", "application/json").setBody(
                """{"subject_id":"historia","topic_id":"la-revolucion","captures":[""" +
                    """{"capture_id":"c1","session_id":"s0","captured_at_ms":5,"thumbnail_url":"/api/x/thumbnail"}]}""",
            ),
        )

        val result = client.listTopicCaptures(backend, "historia", "la-revolucion")

        val captures = (result as BackendResult.Success).value.captures
        assertEquals(listOf(TopicCapture("c1", "s0", 5, "/api/x/thumbnail")), captures)
        val request = server.takeRequest(5, TimeUnit.SECONDS)!!
        assertEquals("GET", request.method)
        assertEquals("/api/subjects/historia/topics/la-revolucion/captures", request.path)
        assertEquals("Bearer sa_tok", request.getHeader("Authorization"))
        assertEquals("laura", request.getHeader("X-SA-User"))
    }

    @Test
    fun `a thumbnail is fetched from this backend only, with the user header`() = runTest {
        server.enqueue(MockResponse().setBody(Buffer().write(byteArrayOf(1, 2, 3))))

        val result = client.captureThumbnail(backend, "/api/subjects/h/topics/t/captures/c1/thumbnail")

        assertArrayEquals(byteArrayOf(1, 2, 3), (result as BackendResult.Success).value)
        val request = server.takeRequest(5, TimeUnit.SECONDS)!!
        assertEquals("laura", request.getHeader("X-SA-User"))
        assertEquals("Bearer sa_tok", request.getHeader("Authorization"))
        val foreign = client.captureThumbnail(backend, "http://evil.example/x.jpg")
        assertEquals(BackendResult.Unreachable("invalid image URL"), foreign)
    }
}
