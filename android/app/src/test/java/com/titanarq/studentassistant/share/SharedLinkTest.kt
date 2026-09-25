package com.titanarq.studentassistant.share

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SharedLinkTest {
    @Test
    fun `a bare link is taken as it is`() {
        assertEquals("https://es.wikipedia.org/wiki/Bastilla", extractSharedUrl("https://es.wikipedia.org/wiki/Bastilla"))
    }

    @Test
    fun `the link is found after the page title a browser puts first`() {
        assertEquals(
            "https://historia.example.edu/bastilla?x=1#fin",
            extractSharedUrl("La Bastilla, 14 de julio\nhttps://historia.example.edu/bastilla?x=1#fin"),
        )
    }

    @Test
    fun `punctuation glued to the end is left out`() {
        assertEquals("https://es.wikipedia.org/wiki/Mercurio_(planeta)", extractSharedUrl("(https://es.wikipedia.org/wiki/Mercurio_(planeta))."))
        assertEquals("http://example.org/a", extractSharedUrl("Mira esto (http://example.org/a)."))
        assertEquals("https://example.org/x", extractSharedUrl("«https://example.org/x»,"))
    }

    @Test
    fun `the first link wins and the scheme is matched in any case`() {
        assertEquals("HTTPS://uno.example/1", extractSharedUrl("HTTPS://uno.example/1 y https://dos.example/2"))
    }

    @Test
    fun `the subject is read when the text has no link`() {
        assertEquals("https://example.org/p", extractSharedUrl("sin enlace", "https://example.org/p"))
    }

    @Test
    fun `no link, another scheme or an overlong one is nothing`() {
        assertNull(extractSharedUrl(null))
        assertNull(extractSharedUrl("apuntes de historia"))
        assertNull(extractSharedUrl("ftp://example.org/a mailto:yo@example.org"))
        assertNull(extractSharedUrl("https://..."))
        assertNull(extractSharedUrl("https://example.org/" + "a".repeat(MAX_SHARED_URL_LENGTH)))
    }
}
