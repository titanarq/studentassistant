package com.titanarq.studentassistant.tutor

import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TutorStreamTest {
    private fun sse(event: String, data: String) = "event: $event\ndata: $data\n\n"

    private fun read(stream: String): Pair<TutorResult<TutorAnswer>, List<TutorProgress>> {
        val progress = mutableListOf<TutorProgress>()
        val result = readTutorStream(Buffer().writeUtf8(stream)) { progress += it }
        return result to progress
    }

    @Test
    fun `the parser joins data lines, names events and ignores comments and other fields`() {
        val parser = SseParser()
        val events = listOf(": keep-alive", "id: 3", "event: reply.delta", "data: {\"a\":", "data: 1}", "", "data:x", "", "")
            .mapNotNull { parser.feed(it) }

        assertEquals(listOf(SseEvent("reply.delta", "{\"a\":\n1}"), SseEvent("message", "x")), events)
    }

    @Test
    fun `an event without data is dropped`() {
        val parser = SseParser()
        assertNull(parser.feed("event: result"))
        assertNull(parser.feed(""))
    }

    @Test
    fun `deltas go to progress and the result is the answer, extra fields ignored`() {
        val (result, progress) = read(
            sse("reply.delta", """{"text": "La derivada ", "attempt": 1}""") +
                sse("reply.delta", """{"text": "es un límite[^p1].", "attempt": 1}""") +
                sse(
                    "result",
                    """{"subject":"calculo","topic":"derivadas","question":"¿Qué es la derivada?",""" +
                        """"reply":"La derivada es un límite[^p1].","refs":[{"label":"p1","kind":"notes",""" +
                        """"text":"Apuntes, página 1","source_id":"src-1","path":"sources/notes/p1.md"}],""" +
                        """"warning":null,"model":"claude"}""",
                ),
        )

        assertEquals(
            listOf(TutorProgress.Delta("La derivada "), TutorProgress.Delta("es un límite[^p1].")),
            progress,
        )
        val answer = (result as TutorResult.Success).value
        assertEquals("¿Qué es la derivada?", answer.question)
        assertEquals("La derivada es un límite[^p1].", answer.reply)
        assertEquals(listOf(TutorRef("p1", "notes", "Apuntes, página 1", "src-1", "sources/notes/p1.md")), answer.refs)
        assertNull(answer.warning)
    }

    @Test
    fun `a restart is reported`() {
        val (_, progress) = read(sse("reply.restart", """{"attempt": 2}""") + sse("result", """{"question":"q","reply":"r"}"""))
        assertEquals(listOf<TutorProgress>(TutorProgress.Restart), progress)
    }

    @Test
    fun `an error event is a refusal with its status, detail and code`() {
        val (result, _) = read(
            sse("reply.delta", """{"text": "x"}""") +
                sse("error", """{"status": 409, "detail": "Se ha alcanzado el límite de gasto del día.", "code": "cost_cap_reached"}"""),
        )

        val refused = result as TutorResult.Refused
        assertEquals(409, refused.status)
        assertEquals("Se ha alcanzado el límite de gasto del día.", refused.detail)
        assertTrue(refused.overCap)
    }

    @Test
    fun `an error event without status or code is a 500 refusal, not over the cap`() {
        val (result, _) = read(sse("error", """{"detail": ["not", "a", "string"]}"""))
        assertEquals(TutorResult.Refused(500, null, null), result)
        assertFalse((result as TutorResult.Refused).overCap)
    }

    @Test
    fun `a stream that ends before its result is interrupted`() {
        val (result, progress) = read(sse("reply.delta", """{"text": "a medias"}""") + "event: result\ndata: {")
        assertEquals(TutorResult.Interrupted, result)
        assertEquals(1, progress.size)
    }

    @Test
    fun `a result that is not an answer is an invalid response`() {
        val (result, _) = read(sse("result", """{"detail": "nada"}"""))
        assertTrue(result is TutorResult.InvalidResponse)
    }
}
