package com.titanarq.studentassistant.home

import java.time.ZoneId
import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Test

class CaptureDateTest {
    private val madrid = ZoneId.of("Europe/Madrid")
    private val es = Locale.forLanguageTag("es")

    @Test
    fun `the date shows the day only, in the student's zone`() {
        // 2026-09-24T23:30:00Z is already the 25th in Madrid (UTC+2).
        val text = formatCaptureDate(1_790_292_600_000, es, madrid)

        assertEquals("25 sept 2026", text.replace(".", ""))
    }
}
