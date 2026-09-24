@file:OptIn(ExperimentalSerializationApi::class)

package com.titanarq.studentassistant.protocol

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonClassDiscriminator

/** Messages the capture client sends over `/ws/sessions/{id}`, discriminated on `type`. */
@Serializable
@JsonClassDiscriminator("type")
sealed interface ClientEvent

/** The only audio the backend accepts in server STT mode (ADR-0001, ADR-0008). */
@Serializable
data class AudioFormat(
    val encoding: AudioEncoding = AudioEncoding.PCM16,
    @SerialName("sample_rate_hz") val sampleRateHz: Int = 16000,
    val channels: Int = 1,
)

@Serializable
enum class AudioEncoding {
    @SerialName("pcm16")
    PCM16,
}

/** Where speech is recognised: on the client's own recognizer or on the backend (ADR-0008). */
@Serializable
enum class SttMode {
    @SerialName("client")
    CLIENT,

    @SerialName("server")
    SERVER,
}

/** What the client can do; the backend picks the STT mode in `hello.ack`. */
@Serializable
data class ClientCapabilities(
    /** The client's preferred STT mode; the backend may still ask for the other one. */
    val stt: SttMode,
    /** Id of the client's own recognizer, e.g. `android-speech`. */
    @SerialName("stt_provider") val sttProvider: String,
    /** Present only when the client can stream audio for server-side STT. */
    @SerialName("audio_format") val audioFormat: AudioFormat? = null,
)

/** First message on the WebSocket: version, capabilities and the clock-sync reading. */
@Serializable
@SerialName("hello")
data class Hello(
    @SerialName("protocol_version") val protocolVersion: String,
    val capabilities: ClientCapabilities,
    /** Client clock in Unix epoch ms when the message was sent. */
    @SerialName("client_time_ms") val clientTimeMs: Long,
) : ClientEvent

/** Interim text of an utterance still being spoken; a later one replaces it. */
@Serializable
@SerialName("transcript.client.partial")
data class TranscriptClientPartial(
    @SerialName("segment_id") val segmentId: String,
    @SerialName("client_start_ms") val clientStartMs: Long,
    @SerialName("client_end_ms") val clientEndMs: Long,
    val text: String,
    val provider: String,
    /** BCP 47, e.g. `es-ES`. */
    val language: String,
    val confidence: Double? = null,
) : ClientEvent

/** The settled text of an utterance; it replaces every partial with the same `segment_id`. */
@Serializable
@SerialName("transcript.client.final")
data class TranscriptClientFinal(
    @SerialName("segment_id") val segmentId: String,
    @SerialName("client_start_ms") val clientStartMs: Long,
    @SerialName("client_end_ms") val clientEndMs: Long,
    val text: String,
    val provider: String,
    val language: String,
    val confidence: Double? = null,
) : ClientEvent

/** Button equivalents of the ADR-0006 voice commands (capture is an upload, not a button event). */
@Serializable
enum class ButtonName {
    @SerialName("next_page")
    NEXT_PAGE,

    @SerialName("important")
    IMPORTANT,

    @SerialName("switch_source")
    SWITCH_SOURCE,

    @SerialName("pause")
    PAUSE,

    @SerialName("resume")
    RESUME,

    @SerialName("end_session")
    END_SESSION,

    @SerialName("web_search")
    WEB_SEARCH,
}

@Serializable
enum class SourceKind {
    @SerialName("book")
    BOOK,

    @SerialName("notes")
    NOTES,

    @SerialName("pdf")
    PDF,
}

/** The student pressed a button; [source] is present exactly with `switch_source`. */
@Serializable
@SerialName("button")
data class Button(
    val button: ButtonName,
    val source: SourceKind? = null,
    @SerialName("client_time_ms") val clientTimeMs: Long,
) : ClientEvent

/** A point on the session timeline the student flagged, with an optional short label. */
@Serializable
@SerialName("marker")
data class Marker(
    @SerialName("client_time_ms") val clientTimeMs: Long,
    val label: String? = null,
) : ClientEvent

/** The client received a server `command` and acted on it (ADR-0006). */
@Serializable
@SerialName("ack")
data class ClientAck(
    @SerialName("command_id") val commandId: String,
    @SerialName("client_time_ms") val clientTimeMs: Long,
) : ClientEvent
