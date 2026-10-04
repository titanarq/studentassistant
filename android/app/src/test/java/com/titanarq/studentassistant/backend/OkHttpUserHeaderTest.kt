package com.titanarq.studentassistant.backend

import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.ClientKind
import com.titanarq.studentassistant.protocol.ErrorCode
import com.titanarq.studentassistant.protocol.PairRequest
import com.titanarq.studentassistant.protocol.SessionEndReason
import com.titanarq.studentassistant.protocol.SessionEndRequest
import com.titanarq.studentassistant.protocol.SessionStartRequest
import com.titanarq.studentassistant.protocol.SubjectCreateRequest
import com.titanarq.studentassistant.protocol.TopicCreateRequest
import com.titanarq.studentassistant.protocol.USER_HEADER
import com.titanarq.studentassistant.protocol.WebPageAddRequest
import com.titanarq.studentassistant.protocol.WebPageVia
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

/** `X-SA-User` on the user-scoped calls and only on them (protocol 1.8, #554). */
class OkHttpUserHeaderTest {
    private val server = MockWebServer()
    private val client = OkHttpBackendClient()
    private lateinit var backend: BackendCredentials

    @Before
    fun setUp() {
        server.start()
        backend = BackendCredentials(server.url("/").toString().trimEnd('/'), "sa_secret", "laura-mendez")
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun taken(): RecordedRequest = server.takeRequest(5, TimeUnit.SECONDS)!!

    private fun answer404() = server.enqueue(MockResponse().setResponseCode(404))

    @Test
    fun `every user-scoped call sends the user header`() = runTest {
        val metadata = CaptureUploadRequest(
            "11111111-1111-4111-8111-111111111111",
            CaptureTrigger.BUTTON,
            null,
            1,
            listOf(CaptureImage("image_0", "image/jpeg", 1, 1, 1)),
        )
        val calls: List<suspend () -> Unit> = listOf(
            { client.listSubjects(backend) },
            { client.createSubject(backend, SubjectCreateRequest("Historia")) },
            { client.listTopics(backend, "historia") },
            { client.createTopic(backend, "historia", TopicCreateRequest("Tema")) },
            { client.startSession(backend, SessionStartRequest("historia", "tema", 1)) },
            { client.resumeSession(backend, "s1") },
            { client.endSession(backend, "s1", SessionEndRequest(1, SessionEndReason.BUTTON)) },
            { client.uploadCapture(backend, "s1", metadata, listOf(CaptureImageBytes(byteArrayOf(1)))) },
            { client.addWebPage(backend, "historia", "tema", WebPageAddRequest("https://example.org/b", WebPageVia.SHARE)) },
        )
        calls.forEach { call ->
            answer404()
            call()
            val request = taken()
            assertEquals(request.path, "laura-mendez", request.getHeader(USER_HEADER))
            assertEquals("Bearer sa_secret", request.getHeader("Authorization"))
        }
    }

    @Test
    fun `credentials without a user send no header`() = runTest {
        answer404()

        client.listSubjects(backend.forUser(null))

        assertNull(taken().getHeader(USER_HEADER))
    }

    @Test
    fun `pair, health, the users list and the photo never send it`() = runTest {
        repeat(4) { answer404() }

        client.pair(backend.baseUrl, PairRequest("ABCD-EFGH", "Pixel", ClientKind.ANDROID, "1.8"))
        client.health(backend.baseUrl)
        client.listUsers(backend)
        client.userPhoto(backend, "/api/users/laura-mendez/photo")

        repeat(4) {
            val request = taken()
            assertNull(request.path, request.getHeader(USER_HEADER))
        }
    }

    @Test
    fun `the photo is fetched from the backend with the bearer token`() = runTest {
        server.enqueue(MockResponse().setBody(okio.Buffer().write(byteArrayOf(1, 2, 3))))

        val result = client.userPhoto(backend, "/api/users/laura-mendez/photo")

        assertArrayEquals(byteArrayOf(1, 2, 3), (result as BackendResult.Success).value)
        val request = taken()
        assertEquals("GET", request.method)
        assertEquals("/api/users/laura-mendez/photo", request.path)
        assertEquals("Bearer sa_secret", request.getHeader("Authorization"))
    }

    @Test
    fun `a photo path of another origin is never fetched`() = runTest {
        val result = client.userPhoto(backend, "http://evil.example/x")

        assertEquals(BackendResult.Unreachable("invalid photo URL"), result)
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `the user codes of an error body are read, no other`() = runTest {
        val bodies = listOf(
            400 to """{"detail":"Di quién eres","code":"user_required"}""",
            404 to """{"detail":"No existe","code":"user_not_found"}""",
            409 to """{"detail":"Tope","code":"cost_cap_reached"}""",
            404 to """{"detail":"No hay"}""",
            400 to "not json",
        )
        bodies.forEach { (status, body) ->
            server.enqueue(MockResponse().setResponseCode(status).setBody(body))
        }

        val results = bodies.map { client.listSubjects(backend) }

        assertEquals(
            listOf(
                BackendResult.HttpError(400, ErrorCode.USER_REQUIRED),
                BackendResult.HttpError(404, ErrorCode.USER_NOT_FOUND),
                BackendResult.HttpError(409),
                BackendResult.HttpError(404),
                BackendResult.HttpError(400),
            ),
            results,
        )
        assertEquals(listOf(true, true, false, false, false), results.map { (it as BackendResult.HttpError).userRejected })
    }
}
