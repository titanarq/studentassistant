@file:OptIn(ExperimentalSerializationApi::class)

package com.titanarq.studentassistant.protocol

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonClassDiscriminator

/**
 * Messages the backend sends over `/ws/sessions/{id}`, discriminated on `type`. `server_time_ms`
 * is the backend clock; `session_*_ms` count ms since the session's `started_at_ms`.
 */
@Serializable
@JsonClassDiscriminator("type")
sealed interface ServerEvent

/** Reply to `hello`: negotiated version, chosen STT mode and clock offset. */
@Serializable
@SerialName("hello.ack")
data class HelloAck(
    /** The version both sides speak (shared MAJOR, lower MINOR). */
    @SerialName("protocol_version") val protocolVersion: String,
    @SerialName("stt_mode") val sttMode: SttMode,
    /** The audio to stream; present exactly when [sttMode] is [SttMode.SERVER]. */
    @SerialName("audio_format") val audioFormat: AudioFormat? = null,
    /** Backend clock minus the client clock read in `hello`; may be negative. */
    @SerialName("clock_offset_ms") val clockOffsetMs: Long,
    @SerialName("server_time_ms") val serverTimeMs: Long,
    /** Since 1.4: domain terms (subject, topic, concepts) a recognizer may be biased towards. */
    @SerialName("vocabulary_hints") val vocabularyHints: List<String>? = null,
) : ServerEvent

/** Interim normalised text of an utterance still being spoken. */
@Serializable
@SerialName("transcript.partial")
data class TranscriptPartial(
    @SerialName("segment_id") val segmentId: String,
    @SerialName("session_start_ms") val sessionStartMs: Long,
    @SerialName("session_end_ms") val sessionEndMs: Long,
    val text: String,
    val language: String,
    val confidence: Double? = null,
) : ServerEvent

/** The settled normalised text; it replaces every partial with the same `segment_id`. */
@Serializable
@SerialName("transcript.final")
data class TranscriptFinal(
    @SerialName("segment_id") val segmentId: String,
    @SerialName("session_start_ms") val sessionStartMs: Long,
    @SerialName("session_end_ms") val sessionEndMs: Long,
    val text: String,
    val language: String,
    val confidence: Double? = null,
) : ServerEvent

@Serializable
enum class CommandName {
    /** Take a burst of stills and upload them with `trigger: command` and the `command_id`. */
    @SerialName("capture_now")
    CAPTURE_NOW,
}

/** A deterministic voice command asks the client to act (ADR-0006); answered by [ClientAck]. */
@Serializable
@SerialName("command")
data class Command(
    @SerialName("command_id") val commandId: String,
    val command: CommandName,
    @SerialName("server_time_ms") val serverTimeMs: Long,
) : ServerEvent

/** Status the client shows the student, such as how many doubts await review. */
@Serializable
@SerialName("notice")
data class Notice(
    @SerialName("pending_count") val pendingCount: Int,
    @SerialName("server_time_ms") val serverTimeMs: Long,
    /** Since 1.4: the session's new vocabulary hints, replacing the previous list. */
    @SerialName("vocabulary_hints") val vocabularyHints: List<String>? = null,
) : ServerEvent

/** The backend stored audio up to a frame `seq` and/or the listed captures (at least one). */
@Serializable
@SerialName("ack")
data class ServerAck(
    @SerialName("audio_seq") val audioSeq: Long? = null,
    @SerialName("capture_ids") val captureIds: List<String>? = null,
    @SerialName("server_time_ms") val serverTimeMs: Long,
) : ServerEvent
