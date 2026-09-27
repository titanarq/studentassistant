package com.titanarq.studentassistant.desk

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class StudyDeskTest {
    @Test
    fun `Construir is the topic's workspace route and Estudiar its study route`() {
        assertEquals(
            "http://192.168.1.20:8000/subjects/historia/topics/revolucion-francesa/workspace",
            workspacePageUrl("http://192.168.1.20:8000", "historia", "revolucion-francesa"),
        )
        assertEquals(
            "http://192.168.1.20:8000/subjects/historia/topics/revolucion-francesa/study",
            studyPageUrl("http://192.168.1.20:8000", "historia", "revolucion-francesa"),
        )
    }

    @Test
    fun `accented and spaced ids are percent-encoded as one path segment each`() {
        assertEquals(
            "http://pc.local:8765/subjects/f%C3%ADsica%2F1/topics/tema%20%3F%231/workspace",
            workspacePageUrl("http://pc.local:8765", "física/1", "tema ?#1"),
        )
        assertEquals(
            "http://pc.local:8765/subjects/f%C3%ADsica/topics/la%20revoluci%C3%B3n/study",
            studyPageUrl("http://pc.local:8765", "física", "la revolución"),
        )
    }

    @Test
    fun `a trailing slash, path or query on the base url is dropped`() {
        assertEquals("http://10.0.0.2:8000/subjects/s/topics/t/workspace", workspacePageUrl("http://10.0.0.2:8000/x/?a=1", "s", "t"))
        assertEquals("http://10.0.0.2:8000/subjects/s/topics/t/study", studyPageUrl("http://10.0.0.2:8000/x/?a=1", "s", "t"))
    }

    @Test
    fun `a base url that is not http is refused`() {
        assertNull(workspacePageUrl("ftp://10.0.0.2", "s", "t"))
        assertNull(studyPageUrl("not a url", "s", "t"))
        assertNull(backendOrigin("192.168.1.20:8000"))
    }

    @Test
    fun `the cookie carries the token to the backend's origin only`() {
        assertEquals("sa_token=sa_abc-_1; Path=/; HttpOnly; SameSite=Strict", tokenCookie("sa_abc-_1"))
        assertEquals("http://192.168.1.20:8000/", backendOrigin("http://192.168.1.20:8000"))
        assertEquals("http://pc.local/", backendOrigin("http://pc.local:80"))
    }

    @Test
    fun `the desk page bundles url, origin and cookie, and hides the token`() {
        val page = deskPage("http://192.168.1.20:8000", "sa_secret", DeskTopic("s1", "t1", "Tema"))!!

        // Construir (the workspace) unless the topic asks for Estudiar.
        assertEquals("http://192.168.1.20:8000/subjects/s1/topics/t1/workspace", page.url)
        assertEquals(
            "http://192.168.1.20:8000/subjects/s1/topics/t1/study",
            deskPage("http://192.168.1.20:8000", "sa_secret", DeskTopic("s1", "t1", "Tema", DeskView.STUDY))!!.url,
        )
        assertEquals("http://192.168.1.20:8000/", page.cookieUrl)
        assertEquals(tokenCookie("sa_secret"), page.cookie)
        assertEquals("sa_secret", page.token)
        assertFalse(page.toString().contains("sa_secret"))
        assertNull(deskPage("nope", "sa_secret", DeskTopic("s1", "t1", "Tema")))
    }

    @Test
    fun `only the backend's own origin stays in the web view`() {
        val base = "http://192.168.1.20:8000"
        assertTrue(isSameOrigin("http://192.168.1.20:8000/subjects/s/topics/t", base))
        assertTrue(isSameOrigin("http://192.168.1.20:8000/", base))
        assertFalse(isSameOrigin("http://192.168.1.20:8001/", base))
        assertFalse(isSameOrigin("https://192.168.1.20:8000/", base))
        assertFalse(isSameOrigin("https://es.wikipedia.org/wiki/Revoluci%C3%B3n", base))
        assertFalse(isSameOrigin("mailto:a@b.c", base))
    }
}
