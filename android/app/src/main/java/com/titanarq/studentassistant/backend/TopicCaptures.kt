package com.titanarq.studentassistant.backend

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * One earlier photo of a topic, as `GET /api/subjects/{s}/topics/{t}/captures` lists it (#580). A
 * read route of the backend, not a versioned body of `protocol/`.
 */
@Serializable
data class TopicCapture(
    @SerialName("capture_id") val captureId: String,
    @SerialName("session_id") val sessionId: String? = null,
    /** Phone time of the capture, Unix epoch ms. */
    @SerialName("captured_at_ms") val capturedAtMs: Long,
    /** The thumbnail route, relative to the backend. */
    @SerialName("thumbnail_url") val thumbnailUrl: String,
)

/** The listing: the topic's captures, oldest first. */
@Serializable
data class TopicCapturesResponse(
    @SerialName("subject_id") val subjectId: String,
    @SerialName("topic_id") val topicId: String,
    val captures: List<TopicCapture>,
)
