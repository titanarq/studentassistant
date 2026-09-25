package com.titanarq.studentassistant.tutor

import com.titanarq.studentassistant.backend.BackendCredentials
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.ServerSocket
import java.util.concurrent.TimeUnit

class OkHttpTutorClientTest {
    private val server = MockWebServer()
    private val client = OkHttpTutorClient()
    private lateinit var backend: BackendCredentials

    @Before
    fun setUp() {
        server.start()
        backend = BackendCredentials(server.url("/").toString().trimEnd('/'), "sa_secret")
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun taken(): RecordedRequest = server.takeRequest(5, TimeUnit.SECONDS)!!

    @Test
    fun `history reads the turns with the bearer token, each id one path segment`() = runTest {
        server.enqueue(
            MockResponse().setBody(
                """{"subject":"c","topic":"d","turns":[{"time":"2026-09-25T10:00:00Z","question":"¿Qué?",""" +
                    """"reply":"Esto[^p1].","refs":[{"label":"p1","kind":"notes","text":"Apuntes, página 1"}],"warning":null}]}""",
            ),
        )

        val result = client.history(backend, "cálculo", "tema 1")

        val turns = (result as TutorResult.Success).value
        assertEquals(1, turns.size)
        assertEquals("2026-09-25T10:00:00Z", turns[0].time)
        assertEquals("Esto[^p1].", turns[0].reply)
        assertEquals("p1", turns[0].refs.single().label)
        val request = taken()
        assertEquals("GET", request.method)
        assertEquals("/api/subjects/c%C3%A1lculo/topics/tema%201/tutor", request.path)
        assertEquals("Bearer sa_secret", request.getHeader("Authorization"))
    }

    @Test
    fun `ask posts the question and streams the answer`() = runTest {
        server.enqueue(
            MockResponse()
                .setHeader("Content-Type", "text/event-stream")
                .setBody(
                    "event: reply.delta\ndata: {\"text\": \"Hola\", \"attempt\": 1}\n\n" +
                        "event: result\ndata: {\"question\": \"¿Qué es?\", \"reply\": \"Hola\", \"refs\": [], \"warning\": \"Ojo\"}\n\n",
                ),
        )
        val progress = mutableListOf<TutorProgress>()

        val result = client.ask(backend, "s", "t", "¿Qué es?", confirmOverCap = true) { progress += it }

        assertEquals(TutorResult.Success(TutorAnswer("¿Qué es?", "Hola", emptyList(), "Ojo")), result)
        assertEquals(listOf<TutorProgress>(TutorProgress.Delta("Hola")), progress)
        val request = taken()
        assertEquals("POST", request.method)
        assertEquals("/api/subjects/s/topics/t/tutor", request.path)
        assertEquals("Bearer sa_secret", request.getHeader("Authorization"))
        assertEquals("text/event-stream", request.getHeader("Accept"))
        val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals(JsonPrimitive("¿Qué es?"), body["question"])
        assertEquals(JsonPrimitive(true), body["confirm_over_cap"])
    }

    @Test
    fun `a refusal before the stream carries the backend's Spanish detail`() = runTest {
        server.enqueue(
            MockResponse().setResponseCode(409)
                .setBody("""{"detail": "Todavía no hay apuntes de este tema: prepáralos antes de preguntar por ellos."}"""),
        )

        val result = client.ask(backend, "s", "t", "¿Algo?")

        assertEquals(
            TutorResult.Refused(409, "Todavía no hay apuntes de este tema: prepáralos antes de preguntar por ellos."),
            result,
        )
    }

    @Test
    fun `a refusal without a readable detail keeps only its status`() = runTest {
        server.enqueue(MockResponse().setResponseCode(401).setBody("nope"))
        assertEquals(TutorResult.Refused(401, null), client.history(backend, "s", "t"))
    }

    @Test
    fun `a history body that is not one is an invalid response`() = runTest {
        server.enqueue(MockResponse().setBody("""{"turns": "no"}"""))
        assertTrue(client.history(backend, "s", "t") is TutorResult.InvalidResponse)
    }

    @Test
    fun `a stream cut short is interrupted`() = runTest {
        server.enqueue(MockResponse().setBody("event: reply.delta\ndata: {\"text\": \"a\"}\n\n"))
        assertEquals(TutorResult.Interrupted, client.ask(backend, "s", "t", "¿Algo?"))
    }

    @Test
    fun `a backend that is not there is unreachable`() = runTest {
        val port = ServerSocket(0).use { it.localPort }
        val result = client.history(BackendCredentials("http://127.0.0.1:$port", "sa_secret"), "s", "t")
        assertTrue(result is TutorResult.Unreachable)
        assertTrue(!(result as TutorResult.Unreachable).reason.contains("sa_secret"))
    }

    @Test
    fun `a base URL that is not http is unreachable without a call`() = runTest {
        assertTrue(client.history(BackendCredentials("ftp://x", "t"), "s", "t") is TutorResult.Unreachable)
        assertEquals(0, server.requestCount)
    }
}
