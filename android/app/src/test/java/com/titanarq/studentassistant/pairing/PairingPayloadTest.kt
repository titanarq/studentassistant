package com.titanarq.studentassistant.pairing

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class PairingPayloadTest {
    private fun valid(result: PairingPayloadResult): PairingPayload =
        (result as? PairingPayloadResult.Valid)?.payload ?: throw AssertionError("expected Valid, got $result")

    private fun error(result: PairingPayloadResult): PairingPayloadError =
        (result as? PairingPayloadResult.Invalid)?.error ?: throw AssertionError("expected Invalid, got $result")

    @Test
    fun `the backend's QR payload is parsed`() {
        // What web/src/pairing/api.ts qrPayload and `studentassistant pair` encode.
        val payload = valid(PairingPayload.parseQr("""{"url":"http://192.168.1.20:8000","code":"ABCD-EFGH"}"""))

        assertEquals(PairingPayload("http://192.168.1.20:8000", "ABCD-EFGH"), payload)
    }

    @Test
    fun `the url loses its trailing slash and the code is trimmed and upper-cased`() {
        val payload = valid(PairingPayload.parseQr(""" {"url": "http://mypc.local:8000/", "code": " abcd-efgh "} """))

        assertEquals("http://mypc.local:8000", payload.url)
        assertEquals("ABCD-EFGH", payload.code)
    }

    @Test
    fun `an https url with a path is kept`() {
        assertEquals("https://example.org/sa", valid(PairingPayload.parseQr("""{"url":"https://example.org/sa/","code":"X"}""")).url)
    }

    @Test
    fun `unknown extra fields are ignored`() {
        val payload = valid(PairingPayload.parseQr("""{"url":"http://10.0.0.2:8000","code":"C","expires_at":1}"""))

        assertEquals("http://10.0.0.2:8000", payload.url)
    }

    @Test
    fun `text that is not the pairing JSON is refused, never thrown`() {
        listOf(
            "",
            "hello",
            "https://example.org",
            "[]",
            """{"url":"http://10.0.0.2:8000"}""",
            """{"code":"ABCD"}""",
            """{"url":1,"code":"ABCD"}""",
            """{"url":"http://10.0.0.2:8000","code":null}""",
            """{"url":"http://10.0.0.2""",
        ).forEach { text ->
            assertEquals(text, PairingPayloadError.NOT_A_PAIRING_QR, error(PairingPayload.parseQr(text)))
        }
    }

    @Test
    fun `a url that is not http or https is refused`() {
        listOf(
            "",
            "192.168.1.20:8000",
            "ftp://192.168.1.20",
            "http://",
            "http://10.0.0.2:8000/?x=1",
            "http://10.0.0.2:8000/#top",
            "http://user:pw@10.0.0.2:8000",
            "javascript:alert(1)",
        ).forEach { url ->
            val json = """{"url":"$url","code":"ABCD"}"""
            assertEquals(url, PairingPayloadError.INVALID_URL, error(PairingPayload.parseQr(json)))
        }
    }

    @Test
    fun `an empty code is refused`() {
        assertEquals(
            PairingPayloadError.MISSING_CODE,
            error(PairingPayload.parseQr("""{"url":"http://10.0.0.2:8000","code":"  "}""")),
        )
    }

    @Test
    fun `manual entry without a scheme is taken as http`() {
        val payload = valid(PairingPayload.fromManualEntry(" 192.168.1.20:8000 ", "abcd-efgh"))

        assertEquals(PairingPayload("http://192.168.1.20:8000", "ABCD-EFGH"), payload)
    }

    @Test
    fun `manual entry keeps an explicit scheme and validates like the QR`() {
        assertEquals("https://mypc.local", valid(PairingPayload.fromManualEntry("https://mypc.local/", "C")).url)
        assertEquals(PairingPayloadError.INVALID_URL, error(PairingPayload.fromManualEntry("", "C")))
        assertEquals(PairingPayloadError.INVALID_URL, error(PairingPayload.fromManualEntry("ftp://x", "C")))
        assertEquals(PairingPayloadError.MISSING_CODE, error(PairingPayload.fromManualEntry("10.0.0.2:8000", "")))
    }

    @Test
    fun `the code never appears in toString`() {
        assertFalse(PairingPayload("http://10.0.0.2:8000", "ABCD-EFGH").toString().contains("ABCD"))
    }
}
