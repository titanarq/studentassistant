package com.titanarq.studentassistant.backend

import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.CaptureUploadResponse
import com.titanarq.studentassistant.protocol.HealthResponse
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

/**
 * A scripted [BackendClient] for view-model tests (fakes over mocks, AGENTS.md). Each endpoint
 * answers with its `var ...Result`, [BackendResult.Unreachable] until a test sets it; every call is
 * appended to [calls] as `"<endpoint> <baseUrl> [<argument>]"` so tests can assert what was asked.
 */
class FakeBackendClient : BackendClient {
    private val notScripted = BackendResult.Unreachable("not scripted")

    var pairResult: BackendResult<PairResponse> = notScripted
    var healthResult: BackendResult<HealthResponse> = notScripted
    var listSubjectsResult: BackendResult<SubjectsListResponse> = notScripted
    var createSubjectResult: BackendResult<Subject> = notScripted
    var listTopicsResult: BackendResult<TopicsListResponse> = notScripted
    var createTopicResult: BackendResult<Topic> = notScripted
    var startSessionResult: BackendResult<Session> = notScripted
    var resumeSessionResult: BackendResult<Session> = notScripted
    var endSessionResult: BackendResult<SessionEndResponse> = notScripted
    var uploadCaptureResult: BackendResult<CaptureUploadResponse> = notScripted

    /** The calls made so far, oldest first. */
    val calls: MutableList<String> = mutableListOf()

    /** The last [PairRequest] sent, if any. */
    var lastPairRequest: PairRequest? = null
        private set

    /** The token of the last authenticated call, if any. */
    var lastToken: String? = null
        private set

    override suspend fun pair(baseUrl: String, request: PairRequest): BackendResult<PairResponse> {
        calls += "pair $baseUrl ${request.pairingCode}"
        lastPairRequest = request
        return pairResult
    }

    override suspend fun health(baseUrl: String): BackendResult<HealthResponse> {
        calls += "health $baseUrl"
        return healthResult
    }

    override suspend fun listSubjects(backend: BackendCredentials): BackendResult<SubjectsListResponse> =
        record("listSubjects", backend) { listSubjectsResult }

    override suspend fun createSubject(
        backend: BackendCredentials,
        request: SubjectCreateRequest,
    ): BackendResult<Subject> = record("createSubject", backend, request.name) { createSubjectResult }

    override suspend fun listTopics(
        backend: BackendCredentials,
        subjectId: String,
    ): BackendResult<TopicsListResponse> = record("listTopics", backend, subjectId) { listTopicsResult }

    override suspend fun createTopic(
        backend: BackendCredentials,
        subjectId: String,
        request: TopicCreateRequest,
    ): BackendResult<Topic> = record("createTopic", backend, subjectId) { createTopicResult }

    override suspend fun startSession(
        backend: BackendCredentials,
        request: SessionStartRequest,
    ): BackendResult<Session> = record("startSession", backend, request.topicId) { startSessionResult }

    override suspend fun resumeSession(backend: BackendCredentials, sessionId: String): BackendResult<Session> =
        record("resumeSession", backend, sessionId) { resumeSessionResult }

    override suspend fun endSession(
        backend: BackendCredentials,
        sessionId: String,
        request: SessionEndRequest,
    ): BackendResult<SessionEndResponse> = record("endSession", backend, sessionId) { endSessionResult }

    override suspend fun uploadCapture(
        backend: BackendCredentials,
        sessionId: String,
        metadata: CaptureUploadRequest,
        images: List<CaptureImageBytes>,
    ): BackendResult<CaptureUploadResponse> =
        record("uploadCapture", backend, metadata.captureId) { uploadCaptureResult }

    private fun <T> record(
        endpoint: String,
        backend: BackendCredentials,
        argument: String? = null,
        result: () -> BackendResult<T>,
    ): BackendResult<T> {
        calls += listOfNotNull(endpoint, backend.baseUrl, argument).joinToString(" ")
        lastToken = backend.token
        return result()
    }
}
