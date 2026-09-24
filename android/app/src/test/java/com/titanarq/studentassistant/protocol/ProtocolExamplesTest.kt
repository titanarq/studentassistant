package com.titanarq.studentassistant.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/** The shared fixtures are the contract test: every example decodes and re-encodes losslessly. */
class ProtocolExamplesTest {
    private val examples = SharedExamples.all()

    @Test
    fun `the shared examples are on the classpath`() {
        assertTrue("found only ${examples.keys}", examples.size >= 28)
    }

    @Test
    fun `every example has a registered codec`() {
        val missing = examples.keys - MESSAGE_CODECS.keys
        assertTrue("examples with no Kotlin counterpart: $missing", missing.isEmpty())
    }

    @Test
    fun `every registered codec has an example`() {
        val stale = MESSAGE_CODECS.keys - examples.keys
        assertTrue("codecs with no shared example: $stale", stale.isEmpty())
    }

    @Test
    fun `every example decodes to its declared class and re-encodes to the same JSON`() {
        val failures = mutableListOf<String>()
        for ((name, text) in examples) {
            val codec = MESSAGE_CODECS[name] ?: continue // reported by the coverage test
            val original: JsonElement = Json.parseToJsonElement(text)
            try {
                val decoded = codec.decode(original)
                if (!codec.messageClass.isInstance(decoded)) {
                    failures += "$name: decoded ${decoded::class.simpleName}, expected ${codec.messageClass.simpleName}"
                    continue
                }
                val reencoded = codec.encode(decoded)
                if (reencoded != original) failures += "$name: re-encoded $reencoded != $original"
            } catch (e: Exception) {
                failures += "$name: ${e::class.simpleName}: ${e.message}"
            }
        }
        if (failures.isNotEmpty()) fail(failures.joinToString("\n"))
    }

    @Test
    fun `hello and hello ack decode their fields`() {
        val hello = decodeClientEvent(SharedExamples.read("client.hello")) as Hello
        assertEquals(PROTOCOL_VERSION, hello.protocolVersion)
        assertEquals(SttMode.CLIENT, hello.capabilities.stt)
        assertEquals(AudioFormat(AudioEncoding.PCM16, 16000, 1), hello.capabilities.audioFormat)
        assertEquals(1790251200000L, hello.clientTimeMs)

        val ack = decodeServerEvent(SharedExamples.read("server.hello.ack")) as HelloAck
        assertEquals(SttMode.CLIENT, ack.sttMode)
        assertEquals(null, ack.audioFormat)
        assertEquals(-350L, ack.clockOffsetMs)
    }

    @Test
    fun `an encoded event carries its wire type`() {
        val json = Json.parseToJsonElement(encodeClientEvent(Marker(clientTimeMs = 1, label = "x")))
        assertEquals("""{"type":"marker","client_time_ms":1,"label":"x"}""", json.toString())
        val ack = Json.parseToJsonElement(encodeServerEvent(Notice(pendingCount = 0, serverTimeMs = 2)))
        assertEquals("""{"type":"notice","pending_count":0,"server_time_ms":2}""", ack.toString())
    }
}
