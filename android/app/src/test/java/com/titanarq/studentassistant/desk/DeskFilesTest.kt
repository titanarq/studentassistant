package com.titanarq.studentassistant.desk

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DeskFilesTest {
    private val base = "http://192.168.1.20:8000"

    /** A fake of `URLUtil.guessFileName` that records what it was asked and answers [answer]. */
    private class FakeGuess(private val answer: String?) {
        val calls = mutableListOf<Triple<String, String?, String?>>()

        fun guess(url: String, contentDisposition: String?, mimeType: String?): String? {
            calls += Triple(url, contentDisposition, mimeType)
            return answer
        }
    }

    @Test
    fun `the pdf upload input asks the picker for pdfs`() {
        assertEquals(listOf("application/pdf"), acceptMimeTypes(listOf("application/pdf,.pdf")))
        assertEquals(listOf("application/pdf"), acceptMimeTypes(listOf("application/pdf", ".pdf")))
    }

    @Test
    fun `mime types and wildcards are kept in order, extensions mapped, case and blanks ignored`() {
        assertEquals(
            listOf("image/*", "image/png", "text/plain"),
            acceptMimeTypes(listOf(" IMAGE/* , .PNG", ".txt,image/png")),
        )
    }

    @Test
    fun `an empty or unmappable accept offers any file`() {
        assertEquals(listOf(ANY_MIME_TYPE), acceptMimeTypes(emptyList()))
        assertEquals(listOf(ANY_MIME_TYPE), acceptMimeTypes(listOf("")))
        assertEquals(listOf(ANY_MIME_TYPE), acceptMimeTypes(listOf(null, ".xyz", "audio", "not a type/")))
    }

    @Test
    fun `a backend download carries the bearer token and the server's file name`() {
        val guess = FakeGuess("revolucion-francesa-deck.apkg")
        val decision = decideDownload(
            url = "$base/api/subjects/s/topics/t/generated/files/deck.apkg",
            contentDisposition = "attachment; filename=\"revolucion-francesa-deck.apkg\"",
            mimeType = "application/octet-stream",
            baseUrl = base,
            token = "sa_secret",
            guessFileName = guess::guess,
        )

        val download = (decision as DownloadDecision.Start).download
        assertEquals("$base/api/subjects/s/topics/t/generated/files/deck.apkg", download.url)
        assertEquals("revolucion-francesa-deck.apkg", download.fileName)
        assertEquals("application/octet-stream", download.mimeType)
        assertEquals(mapOf("Authorization" to "Bearer sa_secret"), download.headers)
        assertEquals(
            listOf(
                Triple(
                    "$base/api/subjects/s/topics/t/generated/files/deck.apkg",
                    "attachment; filename=\"revolucion-francesa-deck.apkg\"",
                    "application/octet-stream",
                ),
            ),
            guess.calls,
        )
    }

    @Test
    fun `the token never shows in a download's string form`() {
        val decision = decideDownload("$base/f.pdf", null, "application/pdf", base, "sa_secret") { _, _, _ -> "f.pdf" }

        assertFalse(decision.toString().contains("sa_secret"))
        assertTrue(decision.toString().contains("Authorization"))
    }

    @Test
    fun `a download that is not on the paired backend is refused without guessing a name`() {
        val guess = FakeGuess("x.pdf")
        for (url in listOf(
            "https://example.com/x.pdf",
            "http://192.168.1.20:8001/x.pdf",
            "https://192.168.1.20:8000/x.pdf",
            "blob:http://192.168.1.20:8000/5b1c",
            "data:application/pdf;base64,AAAA",
        )) {
            assertEquals(DownloadDecision.Refused(url), decideDownload(url, null, null, base, "sa_secret", guess::guess))
        }
        assertTrue(guess.calls.isEmpty())
    }

    @Test
    fun `a blank mime type is left to the download manager`() {
        val decision = decideDownload("$base/f", null, "  ", base, "t") { _, _, _ -> "f.bin" }

        assertEquals(null, (decision as DownloadDecision.Start).download.mimeType)
    }

    @Test
    fun `file names lose separators, reserved characters and leading dots`() {
        assertEquals("tema-slides_deck.pptx", safeFileName("tema-slides/deck.pptx"))
        assertEquals("a_b_c_.pdf", safeFileName("a\\b:c?.pdf"))
        assertEquals("oculto.txt", safeFileName("..oculto.txt"))
        assertEquals(FALLBACK_FILE_NAME, safeFileName(null))
        assertEquals(FALLBACK_FILE_NAME, safeFileName(" .. "))
    }
}
