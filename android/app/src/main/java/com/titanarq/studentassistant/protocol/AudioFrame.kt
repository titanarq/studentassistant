package com.titanarq.studentassistant.protocol

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * One binary audio frame of server STT mode (protocol/README.md "Binary audio frames"): an 18-byte
 * big-endian header (`SAAF`, MAJOR, MINOR, u32 `seq`, u64 client time in ms of the first sample)
 * followed by PCM16, 16 kHz, mono, little-endian samples.
 */
class AudioFrame(
    /** Per-session audio frame counter, acknowledged by the server `ack`'s `audio_seq`. */
    val seq: Long,
    /** Client-clock Unix epoch ms of the first sample. */
    val clientTimeMs: Long,
    /** The samples, one `Short` each. */
    val samples: ShortArray,
    val protocolVersion: String = PROTOCOL_VERSION,
) {
    init {
        require(seq in 0..MAX_SEQ) { "audio frame seq $seq does not fit in a u32" }
        require(clientTimeMs >= 0) { "audio frame client time $clientTimeMs is negative" }
    }

    /** The bytes of one binary WebSocket message. */
    fun encode(): ByteArray {
        val version = parseVersion(protocolVersion)
        val buffer = ByteBuffer.allocate(HEADER_SIZE + samples.size * 2).order(ByteOrder.BIG_ENDIAN)
        buffer.put(MAGIC)
        buffer.put(version.major.toByte())
        buffer.put(version.minor.toByte())
        buffer.putInt(seq.toInt()) // the low 32 bits, i.e. the u32
        buffer.putLong(clientTimeMs)
        buffer.order(ByteOrder.LITTLE_ENDIAN)
        for (sample in samples) buffer.putShort(sample)
        return buffer.array()
    }

    companion object {
        /** ASCII `SAAF`. */
        val MAGIC: ByteArray = byteArrayOf('S'.code.toByte(), 'A'.code.toByte(), 'A'.code.toByte(), 'F'.code.toByte())

        const val HEADER_SIZE: Int = 18

        const val MAX_SEQ: Long = 0xFFFF_FFFFL

        /** Parses one frame; wrong magic, a short or odd-sized message or another MAJOR throws. */
        fun decode(bytes: ByteArray, ours: String = PROTOCOL_VERSION): AudioFrame {
            require(bytes.size >= HEADER_SIZE) { "audio frame of ${bytes.size} bytes is shorter than its header" }
            require((bytes.size - HEADER_SIZE) % 2 == 0) { "audio frame payload is not a whole number of samples" }
            val buffer = ByteBuffer.wrap(bytes).order(ByteOrder.BIG_ENDIAN)
            val magic = ByteArray(4).also { buffer.get(it) }
            require(magic.contentEquals(MAGIC)) { "audio frame magic is not SAAF" }
            val peer = "${buffer.get().toInt() and 0xFF}.${buffer.get().toInt() and 0xFF}"
            checkCompatible(peer, ours)
            val seq = buffer.int.toLong() and MAX_SEQ
            val clientTimeMs = buffer.long
            buffer.order(ByteOrder.LITTLE_ENDIAN)
            val samples = ShortArray((bytes.size - HEADER_SIZE) / 2) { buffer.short }
            return AudioFrame(seq, clientTimeMs, samples, peer)
        }
    }
}
