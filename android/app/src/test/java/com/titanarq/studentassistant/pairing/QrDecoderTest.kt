package com.titanarq.studentassistant.pairing

import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class QrDecoderTest {
    /** A luminance plane with [text] as a QR drawn at the top-left, rows padded to [rowStride]. */
    private fun frame(text: String, size: Int, rowStride: Int): ByteArray {
        val matrix = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, size, size)
        val plane = ByteArray(rowStride * size) { 0xFF.toByte() }
        for (y in 0 until size) {
            for (x in 0 until size) {
                plane[y * rowStride + x] = if (matrix[x, y]) 0 else 0xFF.toByte()
            }
        }
        return plane
    }

    @Test
    fun `a pairing QR is decoded from a luminance plane`() {
        val text = """{"url":"http://192.168.1.20:8000","code":"ABCD-EFGH"}"""

        assertEquals(text, QrDecoder().decode(frame(text, 300, 300), 300, 300))
    }

    @Test
    fun `rows padded beyond the width, as CameraX planes are, still decode`() {
        val text = """{"url":"http://10.0.0.2:8000","code":"WXYZ-2345"}"""

        assertEquals(text, QrDecoder().decode(frame(text, 300, 320), 300, 300, rowStride = 320))
    }

    @Test
    fun `a frame without a QR yields null and the decoder keeps working`() {
        val decoder = QrDecoder()
        assertNull(decoder.decode(ByteArray(200 * 200) { 0x80.toByte() }, 200, 200))

        assertEquals("again", decoder.decode(frame("again", 240, 240), 240, 240))
    }
}
