package com.titanarq.studentassistant.backend

import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.CaptureUploadStatus
import com.titanarq.studentassistant.protocol.ClientKind
import com.titanarq.studentassistant.protocol.PROTOCOL_VERSION
import com.titanarq.studentassistant.protocol.PairRequest
import com.titanarq.studentassistant.protocol.ProtocolJson
import com.titanarq.studentassistant.protocol.SessionEndReason
import com.titanarq.studentassistant.protocol.SessionEndRequest
import com.titanarq.studentassistant.protocol.SessionStartRequest
import com.titanarq.studentassistant.protocol.SubjectCreateRequest
import com.titanarq.studentassistant.protocol.TopicCreateRequest
import com.titanarq.studentassistant.protocol.WebPageAddRequest
import com.titanarq.studentassistant.protocol.WebPageVia
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.ServerSocket
import java.util.concurrent.TimeUnit

class OkHttpBackendClientTest {
    private val server = MockWebServer()
    private val client = OkHttpBackendClient()
    private lateinit var baseUrl: String
    private val token = "sa_secret-token"
    private lateinit var backend: BackendCredentials

    private val sessionJson =
        """{"session_id":"s1","subject_id":"sub1","topic_id":"t1","status":"active","started_at_ms":10,""" +
            """"ws_path":"/ws/sessions/s1","protocol_version":"1.0","received_capture_ids":["c0"]}"""

    @Before
    fun setUp() {
        server.start()
        baseUrl = server.url("/").toString().trimEnd('/')
        backend = BackendCredentials(baseUrl, token)
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun enqueue(body: String, status: Int = 200) {
        server.enqueue(MockResponse().setResponseCode(status).setHeader("Content-Type", "application/json").setBody(body))
    }

    private fun taken(): RecordedRequest = server.takeRequest(5, TimeUnit.SECONDS)!!

    private fun json(text: String): JsonElement = ProtocolJson.parseToJsonElement(text)

    private fun <T> value(result: BackendResult<T>): T =
        (result as? BackendResult.Success)?.value ?: throw AssertionError("expected Success, got $result")

    @Test
    fun `pair posts the request without a bearer and decodes the response`() = runTest {
        enqueue("""{"device_id":"d1","token":"sa_new","protocol_version":"1.0"}""")

        val result = client.pair(baseUrl, PairRequest("ABCD-EFGH", "Pixel 8", ClientKind.ANDROID, "1.0"))

        assertEquals("d1", value(result).deviceId)
        assertEquals("sa_new", value(result).token)
        val request = taken()
        assertEquals("POST", request.method)
        assertEquals("/api/pair", request.path)
        assertNull(request.getHeader("Authorization"))
        assertEquals(
            json("""{"pairing_code":"ABCD-EFGH","device_name":"Pixel 8","client_kind":"android","protocol_version":"1.0"}"""),
            json(request.body.readUtf8()),
        )
    }

    @Test
    fun `pair refuses a backend with another MAJOR version`() = runTest {
        enqueue("""{"device_id":"d1","token":"sa_new","protocol_version":"2.0"}""")

        val result = client.pair(baseUrl, PairRequest("C", "Pixel", ClientKind.ANDROID, "1.0"))

        assertEquals(BackendResult.IncompatibleVersion(peer = "2.0", ours = PROTOCOL_VERSION), result)
    }

    @Test
    fun `a newer MINOR of the same MAJOR is accepted`() = runTest {
        enqueue("""{"device_id":"d1","token":"sa_new","protocol_version":"1.7"}""")

        value(client.pair(baseUrl, PairRequest("C", "Pixel", ClientKind.ANDROID, "1.0")))
    }

    @Test
    fun `a malformed protocol_version is an invalid response`() = runTest {
        enqueue("""{"status":"ok","protocol_version":"one","server_time_ms":5}""")

        assertTrue(client.health(baseUrl) is BackendResult.InvalidResponse)
    }

    @Test
    fun `health is an unauthenticated GET`() = runTest {
        enqueue("""{"status":"ok","protocol_version":"1.0","server_time_ms":123}""")

        assertEquals(123L, value(client.health(baseUrl)).serverTimeMs)
        val request = taken()
        assertEquals("GET", request.method)
        assertEquals("/api/health", request.path)
        assertNull(request.getHeader("Authorization"))
    }

    @Test
    fun `subjects list and create send the bearer`() = runTest {
        enqueue("""{"subjects":[{"subject_id":"sub1","name":"Historia"}]}""")
        enqueue("""{"subject_id":"sub2","name":"Física"}""")

        assertEquals("Historia", value(client.listSubjects(backend)).subjects.single().name)
        assertEquals("sub2", value(client.createSubject(backend, SubjectCreateRequest("Física"))).subjectId)

        val list = taken()
        assertEquals("GET", list.method)
        assertEquals("/api/subjects", list.path)
        assertEquals("Bearer $token", list.getHeader("Authorization"))
        val create = taken()
        assertEquals("POST", create.method)
        assertEquals("/api/subjects", create.path)
        assertEquals("Bearer $token", create.getHeader("Authorization"))
        assertEquals(json("""{"name":"Física"}"""), json(create.body.readUtf8()))
    }

    @Test
    fun `topics list and create address the subject, percent-encoding its id`() = runTest {
        enqueue("""{"subject_id":"a b","topics":[{"topic_id":"t1","subject_id":"a b","name":"Tema 1","open_session_id":"s1"}]}""")
        enqueue("""{"topic_id":"t2","subject_id":"a b","name":"Tema 2"}""")

        assertEquals("s1", value(client.listTopics(backend, "a b")).topics.single().openSessionId)
        assertNull(value(client.createTopic(backend, "a b", TopicCreateRequest("Tema 2"))).openSessionId)

        val list = taken()
        assertEquals("GET", list.method)
        assertEquals("/api/subjects/a%20b/topics", list.path)
        assertEquals("Bearer $token", list.getHeader("Authorization"))
        val create = taken()
        assertEquals("POST", create.method)
        assertEquals("/api/subjects/a%20b/topics", create.path)
        assertEquals("Bearer $token", create.getHeader("Authorization"))
        assertEquals(json("""{"name":"Tema 2"}"""), json(create.body.readUtf8()))
    }

    @Test
    fun `sessions start, resume and end`() = runTest {
        enqueue(sessionJson)
        enqueue(sessionJson)
        enqueue("""{"session_id":"s1","status":"ended","ended_at_ms":99}""")

        assertEquals("s1", value(client.startSession(backend, SessionStartRequest("sub1", "t1", 7))).sessionId)
        assertEquals(listOf("c0"), value(client.resumeSession(backend, "s1")).receivedCaptureIds)
        assertEquals(99L, value(client.endSession(backend, "s1", SessionEndRequest(8, SessionEndReason.BUTTON))).endedAtMs)

        val start = taken()
        assertEquals("POST", start.method)
        assertEquals("/api/sessions", start.path)
        assertEquals("Bearer $token", start.getHeader("Authorization"))
        assertEquals(json("""{"subject_id":"sub1","topic_id":"t1","client_time_ms":7}"""), json(start.body.readUtf8()))
        val resume = taken()
        assertEquals("POST", resume.method)
        assertEquals("/api/sessions/s1/resume", resume.path)
        assertEquals("Bearer $token", resume.getHeader("Authorization"))
        assertEquals(0L, resume.bodySize)
        val end = taken()
        assertEquals("POST", end.method)
        assertEquals("/api/sessions/s1/end", end.path)
        assertEquals("Bearer $token", end.getHeader("Authorization"))
        assertEquals(json("""{"client_time_ms":8,"reason":"button"}"""), json(end.body.readUtf8()))
    }

    @Test
    fun `a session on another MAJOR version is refused`() = runTest {
        enqueue(sessionJson.replace("\"1.0\"", "\"2.1\""))

        assertEquals(BackendResult.IncompatibleVersion("2.1", PROTOCOL_VERSION), client.resumeSession(backend, "s1"))
    }

    @Test
    fun `a capture burst is uploaded as metadata plus image_N parts`() = runTest {
        enqueue("""{"capture_id":"c1","session_id":"s1","status":"stored","image_count":2,"received_at_ms":50}""")
        val metadata = CaptureUploadRequest(
            captureId = "c1",
            trigger = CaptureTrigger.BUTTON,
            clientTimeMs = 40,
            images = listOf(
                CaptureImage("image_0", "image/jpeg", 4000, 3000, 41),
                CaptureImage("image_1", "image/png", 10, 20, 42),
            ),
        )

        val result = client.uploadCapture(
            backend,
            "s1",
            metadata,
            listOf(CaptureImageBytes(byteArrayOf(1, 2, 3)), CaptureImageBytes(byteArrayOf(9))),
        )

        assertEquals(CaptureUploadStatus.STORED, value(result).status)
        val request = taken()
        assertEquals("POST", request.method)
        assertEquals("/api/sessions/s1/captures", request.path)
        assertEquals("Bearer $token", request.getHeader("Authorization"))
        val contentType = request.getHeader("Content-Type")!!
        assertTrue(contentType, contentType.startsWith("multipart/form-data; boundary="))
        val parts = MultipartParts.parse(request.body.readByteArray(), contentType.substringAfter("boundary="))
        assertEquals(listOf("metadata", "image_0", "image_1"), parts.map { it.name })
        assertEquals(json(ProtocolJson.encodeToString(CaptureUploadRequest.serializer(), metadata)), json(parts[0].body.decodeToString()))
        assertEquals("c1", json(parts[0].body.decodeToString()).jsonObject["capture_id"]!!.jsonPrimitive.content)
        assertTrue(parts[0].contentType!!.startsWith("application/json"))
        assertEquals("image/jpeg", parts[1].contentType)
        assertEquals(listOf<Byte>(1, 2, 3), parts[1].body.toList())
        assertEquals("image/png", parts[2].contentType)
        assertEquals(listOf<Byte>(9), parts[2].body.toList())
    }

    @Test
    fun `a duplicate capture is a success`() = runTest {
        enqueue("""{"capture_id":"c1","session_id":"s1","status":"duplicate","image_count":1,"received_at_ms":50}""")
        val metadata = CaptureUploadRequest(
            "c1",
            CaptureTrigger.COMMAND,
            commandId = "cmd1",
            clientTimeMs = 40,
            images = listOf(CaptureImage("image_0", "image/webp", 1, 1, 41)),
        )

        val result = client.uploadCapture(backend, "s1", metadata, listOf(CaptureImageBytes(byteArrayOf(0))))

        assertEquals(CaptureUploadStatus.DUPLICATE, value(result).status)
    }

    @Test
    fun `a shared web page is posted to the topic's web-pages`() = runTest {
        enqueue(
            """{"source_id":"sources/web/001-la-bastilla.md","vault_id":"subjects/h/topics/t/sources/web/001-la-bastilla.md",""" +
                """"title":"La Bastilla","url":"https://example.org/b","already_kept":false}""",
            status = 201,
        )

        val result = client.addWebPage(backend, "historia", "la revolución", WebPageAddRequest("https://example.org/b", WebPageVia.SHARE))

        assertEquals("sources/web/001-la-bastilla.md", value(result).sourceId)
        assertFalse(value(result).alreadyKept)
        val request = taken()
        assertEquals("POST", request.method)
        assertEquals("/api/subjects/historia/topics/la%20revoluci%C3%B3n/web-pages", request.path)
        assertEquals("Bearer $token", request.getHeader("Authorization"))
        assertEquals(json("""{"url":"https://example.org/b","via":"share"}"""), json(request.body.readUtf8()))
    }

    @Test
    fun `a non-2xx answer is an HttpError with its status`() = runTest {
        enqueue("""{"detail":"unauthorized"}""", status = 401)
        enqueue("""{"detail":"nope"}""", status = 404)
        enqueue("boom", status = 500)

        assertEquals(BackendResult.HttpError(401), client.pair(baseUrl, PairRequest("C", "P", ClientKind.ANDROID, "1.0")))
        assertEquals(BackendResult.HttpError(404), client.listTopics(backend, "missing"))
        assertEquals(BackendResult.HttpError(500), client.listSubjects(backend))
    }

    @Test
    fun `a body that breaks the contract is an InvalidResponse`() = runTest {
        enqueue("not json")
        enqueue("""{"subjects":[],"unexpected":1}""")
        // The backend's current `{status, version}` health body (docs/modules/server.md) is not v1.
        enqueue("""{"status":"ok","version":"0.1.0"}""")

        assertTrue(client.listSubjects(backend) is BackendResult.InvalidResponse)
        assertTrue(client.listSubjects(backend) is BackendResult.InvalidResponse)
        assertTrue(client.health(baseUrl) is BackendResult.InvalidResponse)
    }

    @Test
    fun `a backend that is not listening is Unreachable, and the reason never carries the token`() = runTest {
        val port = ServerSocket(0).use { it.localPort }

        val result = client.listSubjects(BackendCredentials("http://127.0.0.1:$port", token))

        assertTrue(result.toString(), result is BackendResult.Unreachable)
        assertFalse(result.toString().contains(token))
    }

    @Test
    fun `a base url that is not http is Unreachable, not thrown`() = runTest {
        assertTrue(client.health("not a url") is BackendResult.Unreachable)
    }

    @Test
    fun `urls are built under the base url without doubled slashes`() {
        assertEquals(
            "http://10.0.0.2:8000/api/health",
            OkHttpBackendClient.buildUrl("http://10.0.0.2:8000/", listOf("api", "health")).toString(),
        )
        assertEquals(
            "https://h/sa/api/sessions/s%2F1/end",
            OkHttpBackendClient.buildUrl("https://h/sa", listOf("api", "sessions", "s/1", "end")).toString(),
        )
    }

    @Test
    fun `credentials never print their token`() {
        assertFalse(backend.toString().contains(token))
    }
}
