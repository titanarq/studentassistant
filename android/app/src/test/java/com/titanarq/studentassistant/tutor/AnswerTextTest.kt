package com.titanarq.studentassistant.tutor

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AnswerTextTest {
    @Test
    fun `footnote marks are shown as bracketed labels`() {
        assertEquals("Es un límite[p1] y una pendiente[b-2].", shownText("Es un límite[^p1] y una pendiente[^b-2]."))
    }

    @Test
    fun `the spoken text drops marks, doubts' brackets and Markdown`() {
        val reply = "## Derivada\nLa **derivada** es un límite [^p1]. Se escribe `f'(x)` [[?o dy/dx]] [^p2]."
        assertEquals("Derivada La derivada es un límite. Se escribe f'(x) o dy/dx.", spokenText(reply))
    }

    @Test
    fun `a footnote definition is not taken for a mark`() {
        assertEquals("[^p1]: Apuntes", spokenText("[^p1]: Apuntes"))
    }

    @Test
    fun `short texts are one chunk`() {
        assertEquals(listOf("Hola."), speechChunks("  Hola.  ", 4000))
        assertEquals(emptyList<String>(), speechChunks("   ", 10))
    }

    @Test
    fun `long texts are cut at sentence ends, then spaces, then anywhere`() {
        assertEquals(listOf("Uno dos.", "Tres cuatro."), speechChunks("Uno dos. Tres cuatro.", 14))
        assertEquals(listOf("uno dos", "tres"), speechChunks("uno dos tres", 8))
        assertEquals(listOf("abcd", "efgh", "ij"), speechChunks("abcdefghij", 4))
        val chunks = speechChunks("Frase larga. ".repeat(500), 4000)
        assertTrue(chunks.all { it.length <= 4000 })
        assertEquals("Frase larga. ".repeat(500).trim(), chunks.joinToString(" "))
    }
}
