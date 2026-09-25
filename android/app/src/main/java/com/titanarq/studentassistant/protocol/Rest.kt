package com.titanarq.studentassistant.protocol

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

// Bodies of the REST endpoints under /api (protocol/README.md). Every endpoint except
// GET /api/health and POST /api/pair needs `Authorization: Bearer <token>`. Times are Unix epoch
// ms: `client_time_ms` on the client's clock, `*_at_ms` and `server_time_ms` on the backend's.

// POST /api/pair

@Serializable
enum class ClientKind {
    @SerialName("web")
    WEB,

    @SerialName("android")
    ANDROID,
}

/** Exchanges the one-time code shown in the pairing QR for a long-lived bearer token. */
@Serializable
data class PairRequest(
    @SerialName("pairing_code") val pairingCode: String,
    @SerialName("device_name") val deviceName: String,
    @SerialName("client_kind") val clientKind: ClientKind,
    @SerialName("protocol_version") val protocolVersion: String,
)

@Serializable
data class PairResponse(
    @SerialName("device_id") val deviceId: String,
    /** Sent as `Authorization: Bearer <token>`; never logged. */
    val token: String,
    @SerialName("protocol_version") val protocolVersion: String,
) {
    override fun toString(): String =
        "PairResponse(deviceId=$deviceId, token=<redacted>, protocolVersion=$protocolVersion)"
}

// GET /api/health

@Serializable
enum class HealthStatus {
    @SerialName("ok")
    OK,
}

@Serializable
data class HealthResponse(
    val status: HealthStatus,
    @SerialName("protocol_version") val protocolVersion: String,
    @SerialName("server_time_ms") val serverTimeMs: Long,
)

// GET /api/subjects, POST /api/subjects

@Serializable
data class Subject(
    @SerialName("subject_id") val subjectId: String,
    val name: String,
)

@Serializable
data class SubjectsListResponse(val subjects: List<Subject>)

@Serializable
data class SubjectCreateRequest(val name: String)

// GET /api/subjects/{subject_id}/topics, POST /api/subjects/{subject_id}/topics

@Serializable
data class Topic(
    @SerialName("topic_id") val topicId: String,
    @SerialName("subject_id") val subjectId: String,
    val name: String,
    /** The session still open on this topic, if any: resume it instead of starting one. */
    @SerialName("open_session_id") val openSessionId: String? = null,
    /** Since 1.1: start of the topic's latest session, backend clock, epoch ms; null when unknown. */
    @SerialName("last_session_at_ms") val lastSessionAtMs: Long? = null,
    /** Since 1.1: open pending-review items (doubts awaiting the student); null when unknown. */
    @SerialName("pending_count") val pendingCount: Int? = null,
)

@Serializable
data class TopicsListResponse(
    @SerialName("subject_id") val subjectId: String,
    val topics: List<Topic>,
)

@Serializable
data class TopicCreateRequest(val name: String)

// POST /api/sessions, POST /api/sessions/{id}/resume, POST /api/sessions/{id}/end

/** A session is about exactly one topic of one subject, fixed here. */
@Serializable
data class SessionStartRequest(
    @SerialName("subject_id") val subjectId: String,
    @SerialName("topic_id") val topicId: String,
    @SerialName("client_time_ms") val clientTimeMs: Long,
)

@Serializable
enum class SessionActiveStatus {
    @SerialName("active")
    ACTIVE,
}

/** An open session, returned by start and resume. */
@Serializable
data class Session(
    @SerialName("session_id") val sessionId: String,
    @SerialName("subject_id") val subjectId: String,
    @SerialName("topic_id") val topicId: String,
    val status: SessionActiveStatus,
    @SerialName("started_at_ms") val startedAtMs: Long,
    /** Where to open the WebSocket, always `/ws/sessions/{session_id}`. */
    @SerialName("ws_path") val wsPath: String,
    @SerialName("protocol_version") val protocolVersion: String,
    /** Captures the backend already stored, so a resuming client re-uploads only the rest. */
    @SerialName("received_capture_ids") val receivedCaptureIds: List<String> = emptyList(),
)

@Serializable
enum class SessionEndReason {
    @SerialName("button")
    BUTTON,

    @SerialName("command")
    COMMAND,
}

@Serializable
data class SessionEndRequest(
    @SerialName("client_time_ms") val clientTimeMs: Long,
    val reason: SessionEndReason,
)

@Serializable
enum class SessionEndedStatus {
    @SerialName("ended")
    ENDED,
}

@Serializable
data class SessionEndResponse(
    @SerialName("session_id") val sessionId: String,
    val status: SessionEndedStatus,
    @SerialName("ended_at_ms") val endedAtMs: Long,
)

// POST /api/sessions/{id}/captures (multipart/form-data)

@Serializable
enum class CaptureTrigger {
    @SerialName("button")
    BUTTON,

    @SerialName("command")
    COMMAND,
}

@Serializable
data class CaptureImage(
    /** Name of the multipart part carrying this image's bytes, `image_<n>`. */
    val part: String,
    /** `image/jpeg`, `image/png` or `image/webp`. */
    @SerialName("content_type") val contentType: String,
    @SerialName("width_px") val widthPx: Int,
    @SerialName("height_px") val heightPx: Int,
    @SerialName("client_time_ms") val clientTimeMs: Long,
)

/**
 * The `metadata` JSON part of a capture burst; the images travel as sibling parts. Re-sending a
 * [captureId] already stored is answered with `duplicate`, so retries are free.
 */
@Serializable
data class CaptureUploadRequest(
    @SerialName("capture_id") val captureId: String,
    val trigger: CaptureTrigger,
    /** The `command` that asked for this capture, when [trigger] is [CaptureTrigger.COMMAND]. */
    @SerialName("command_id") val commandId: String? = null,
    @SerialName("client_time_ms") val clientTimeMs: Long,
    val images: List<CaptureImage>,
)

@Serializable
enum class CaptureUploadStatus {
    @SerialName("stored")
    STORED,

    @SerialName("duplicate")
    DUPLICATE,
}

@Serializable
data class CaptureUploadResponse(
    @SerialName("capture_id") val captureId: String,
    @SerialName("session_id") val sessionId: String,
    val status: CaptureUploadStatus,
    @SerialName("image_count") val imageCount: Int,
    @SerialName("received_at_ms") val receivedAtMs: Long,
)
