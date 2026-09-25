package com.titanarq.studentassistant.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class AudioFrameTest {
    @Test
    fun `encodes the big-endian header and little-endian samples`() {
        val bytes = AudioFrame(seq = 0x01020304, clientTimeMs = 0x0000_0190_0000_00FFL, samples = shortArrayOf(1, -2, 0x1234))
            .encode()
        val expected = byteArrayOf(
            'S'.code.toByte(), 'A'.code.toByte(), 'A'.code.toByte(), 'F'.code.toByte(),
            1, 3, // protocol_version 1.3
            1, 2, 3, 4, // seq
            0, 0, 1, 0x90.toByte(), 0, 0, 0, 0xFF.toByte(), // client time ms
            1, 0, // 1
            0xFE.toByte(), 0xFF.toByte(), // -2
            0x34, 0x12, // 0x1234
        )
        assertArrayEquals(expected, bytes)
        assertEquals(AudioFrame.HEADER_SIZE + 6, bytes.size)
    }

    @Test
    fun `decodes what it encodes, including a u32 seq above Int_MAX_VALUE`() {
        val frame = AudioFrame(0xFFFF_FFF0L, 1_727_000_000_000L, ShortArray(1600) { (it - 800).toShort() })
        val decoded = AudioFrame.decode(frame.encode())
        assertEquals(frame.seq, decoded.seq)
        assertEquals(frame.clientTimeMs, decoded.clientTimeMs)
        assertEquals(frame.samples.toList(), decoded.samples.toList())
        assertEquals("1.3", decoded.protocolVersion)
    }

    @Test
    fun `refuses a wrong magic, another MAJOR, a short or ragged frame and an out-of-range seq`() {
        val good = AudioFrame(1, 2, shortArrayOf(3)).encode()
        assertThrows(IllegalArgumentException::class.java) {
            AudioFrame.decode(good.copyOf().also { it[0] = 'X'.code.toByte() })
        }
        assertThrows(IncompatibleProtocolVersionException::class.java) {
            AudioFrame.decode(good.copyOf().also { it[4] = 2 })
        }
        // Another MINOR is accepted.
        assertEquals("1.7", AudioFrame.decode(good.copyOf().also { it[5] = 7 }).protocolVersion)
        assertThrows(IllegalArgumentException::class.java) { AudioFrame.decode(good.copyOf(10)) }
        assertThrows(IllegalArgumentException::class.java) { AudioFrame.decode(good.copyOf(good.size - 1)) }
        assertThrows(IllegalArgumentException::class.java) { AudioFrame(AudioFrame.MAX_SEQ + 1, 0, shortArrayOf()) }
    }
}
