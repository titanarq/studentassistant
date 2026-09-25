package com.titanarq.studentassistant.protocol

import kotlinx.serialization.SerializationException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class EventDecodingTest {
    @Test
    fun `an unknown client type fails naming the type`() {
        val e = assertThrows(SerializationException::class.java) {
            decodeClientEvent("""{"type":"teleport","client_time_ms":1}""")
        }
        assertTrue(e.message, e.message!!.contains("teleport"))
    }

    @Test
    fun `an unknown server type fails naming the type`() {
        val e = assertThrows(SerializationException::class.java) {
            decodeServerEvent("""{"type":"capture_later","server_time_ms":1}""")
        }
        assertTrue(e.message, e.message!!.contains("capture_later"))
    }

    @Test
    fun `a missing type fails`() {
        assertThrows(SerializationException::class.java) { decodeServerEvent("""{"pending_count":1,"server_time_ms":1}""") }
        assertThrows(SerializationException::class.java) { decodeClientEvent("""{"client_time_ms":1}""") }
    }

    @Test
    fun `a client-only type is not a server event`() {
        assertThrows(SerializationException::class.java) {
            decodeServerEvent(SharedExamples.read("client.marker"))
        }
    }

    @Test
    fun `an unknown field is a contract violation`() {
        assertThrows(SerializationException::class.java) {
            decodeServerEvent("""{"type":"notice","pending_count":1,"server_time_ms":1,"extra":true}""")
        }
    }

    @Test
    fun `both ack types decode into their own hierarchy`() {
        assertTrue(decodeClientEvent(SharedExamples.read("client.ack")) is ClientAck)
        assertTrue(decodeServerEvent(SharedExamples.read("server.ack")) is ServerAck)
    }

    /** Exhaustive `when` over the sealed hierarchy: adding or removing a member breaks compilation. */
    private fun describe(event: ServerEvent): String = when (event) {
        is HelloAck -> "hello.ack"
        is TranscriptPartial -> "transcript.partial"
        is TranscriptFinal -> "transcript.final"
        is Command -> "command"
        is Notice -> "notice"
        is SttStatus -> "stt.status"
        is ServerAck -> "ack"
    }

    private fun describe(event: ClientEvent): String = when (event) {
        is Hello -> "hello"
        is TranscriptClientPartial -> "transcript.client.partial"
        is TranscriptClientFinal -> "transcript.client.final"
        is Button -> "button"
        is Marker -> "marker"
        is ClientAck -> "ack"
    }

    @Test
    fun `every example event maps to its wire type`() {
        for ((name, text) in SharedExamples.all()) {
            when {
                name.startsWith("server.") -> assertEquals(name.removePrefix("server."), describe(decodeServerEvent(text)))
                name.startsWith("client.") -> assertEquals(name.removePrefix("client."), describe(decodeClientEvent(text)))
            }
        }
    }
}
