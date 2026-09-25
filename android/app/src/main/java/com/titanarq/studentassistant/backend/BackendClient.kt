package com.titanarq.studentassistant.backend

import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.CaptureUploadResponse
import com.titanarq.studentassistant.protocol.HealthResponse
import com.titanarq.studentassistant.protocol.NotesGenerationStatus
import com.titanarq.studentassistant.protocol.PairRequest
import com.titanarq.studentassistant.protocol.PairResponse
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionEndRequest
import com.titanarq.studentassistant.protocol.SessionEndResponse
import com.titanarq.studentassistant.protocol.SessionStartRequest
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.SubjectCreateRequest
import com.titanarq.studentassistant.protocol.SubjectsListResponse
import com.titanarq.studentassistant.protocol.Topic
import com.titanarq.studentassistant.protocol.TopicCreateRequest
import com.titanarq.studentassistant.protocol.TopicsListResponse
import com.titanarq.studentassistant.protocol.WebPageAddRequest
import com.titanarq.studentassistant.protocol.WebPageAddResponse

/**
 * Where and as whom to call a paired backend: its base URL (`http://host:port`, no trailing
 * slash) and the bearer token pairing returned. The token never appears in [toString].
 */
data class BackendCredentials(val baseUrl: String, val token: String) {
    override fun toString(): String = "BackendCredentials(baseUrl=$baseUrl, token=<redacted>)"
}

/** The bytes of one image of a capture burst; its part name and type come from the metadata. */
class CaptureImageBytes(val bytes: ByteArray) {
    override fun toString(): String = "CaptureImageBytes(${bytes.size} bytes)"
}

/**
 * The outcome of one backend call. Failures are values, never exceptions, so the UI can show a
 * Spanish message for each kind.
 */
sealed interface BackendResult<out T> {
    data class Success<T>(val value: T) : BackendResult<T>

    sealed interface Failure : BackendResult<Nothing>

    /** The backend answered with a non-2xx status. */
    data class HttpError(val status: Int) : Failure

    /** No answer: bad address, connection refused, timeout, the phone is not on the LAN, ... */
    data class Unreachable(val reason: String) : Failure

    /** The backend speaks another protocol MAJOR version ([peer]); this app speaks [ours]. */
    data class IncompatibleVersion(val peer: String, val ours: String) : Failure

    /** The backend answered 2xx with a body that does not match the protocol v1 contract. */
    data class InvalidResponse(val reason: String) : Failure
}

/**
 * Every REST endpoint of capture protocol v1 (`protocol/README.md` "REST"). [pair] and [health]
 * are unauthenticated; every other call sends `Authorization: Bearer <token>`.
 *
 * Responses that carry a `protocol_version` (pair, health, session start/resume) are checked
 * against this app's version: a different MAJOR is [BackendResult.IncompatibleVersion].
 */
interface BackendClient {
    /** `POST /api/pair`: exchanges the QR's one-time code for a device id and bearer token. */
    suspend fun pair(baseUrl: String, request: PairRequest): BackendResult<PairResponse>

    /** `GET /api/health`: reachability and the backend's protocol version. */
    suspend fun health(baseUrl: String): BackendResult<HealthResponse>

    /** `GET /api/subjects`. */
    suspend fun listSubjects(backend: BackendCredentials): BackendResult<SubjectsListResponse>

    /** `POST /api/subjects`. */
    suspend fun createSubject(
        backend: BackendCredentials,
        request: SubjectCreateRequest,
    ): BackendResult<Subject>

    /** `GET /api/subjects/{subject_id}/topics`. */
    suspend fun listTopics(
        backend: BackendCredentials,
        subjectId: String,
    ): BackendResult<TopicsListResponse>

    /** `POST /api/subjects/{subject_id}/topics`. */
    suspend fun createTopic(
        backend: BackendCredentials,
        subjectId: String,
        request: TopicCreateRequest,
    ): BackendResult<Topic>

    /** `POST /api/sessions`. */
    suspend fun startSession(
        backend: BackendCredentials,
        request: SessionStartRequest,
    ): BackendResult<Session>

    /** `POST /api/sessions/{id}/resume`. */
    suspend fun resumeSession(backend: BackendCredentials, sessionId: String): BackendResult<Session>

    /** `POST /api/sessions/{id}/end`. */
    suspend fun endSession(
        backend: BackendCredentials,
        sessionId: String,
        request: SessionEndRequest,
    ): BackendResult<SessionEndResponse>

    /**
     * `POST /api/sessions/{id}/captures` as `multipart/form-data`: the `metadata` part holds
     * [metadata] as JSON and `images[i]` travels as the part named `metadata.images[i].part` with
     * its `content_type`. [images] must have one entry per `metadata.images`.
     */
    suspend fun uploadCapture(
        backend: BackendCredentials,
        sessionId: String,
        metadata: CaptureUploadRequest,
        images: List<CaptureImageBytes>,
    ): BackendResult<CaptureUploadResponse>

    /**
     * `POST /api/subjects/{subject_id}/topics/{topic_id}/web-pages`: the backend fetches the page
     * and stores it as a web source of the topic (201), or answers the snapshot it already has
     * (200, `already_kept`). The fetch goes through Claude, so the call may take a while.
     */
    suspend fun addWebPage(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
        request: WebPageAddRequest,
    ): BackendResult<WebPageAddResponse>

    /**
     * `GET /api/subjects/{subject_id}/topics/{topic_id}/notes/generation` (protocol 1.6): the
     * topic's latest notes generation since the backend started, polled while it runs.
     */
    suspend fun notesGeneration(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
    ): BackendResult<NotesGenerationStatus>
}
