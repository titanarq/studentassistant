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
import com.titanarq.studentassistant.protocol.User
import com.titanarq.studentassistant.protocol.UserUpdateRequest
import com.titanarq.studentassistant.protocol.UsersListResponse
import com.titanarq.studentassistant.protocol.WebPageAddRequest
import com.titanarq.studentassistant.protocol.WebPageAddResponse

/**
 * A scripted [BackendClient] for view-model tests (fakes over mocks, AGENTS.md). Each endpoint
 * answers with its `var ...Result`, [BackendResult.Unreachable] until a test sets it; every call is
 * appended to [calls] as `"<endpoint> <baseUrl> [<argument>]"` so tests can assert what was asked.
 */
class FakeBackendClient : BackendClient {
    private val notScripted = BackendResult.Unreachable("not scripted")

    var pairResult: BackendResult<PairResponse> = notScripted
    var healthResult: BackendResult<HealthResponse> = notScripted
    var listUsersResult: BackendResult<UsersListResponse> = notScripted
    var userPhotoResult: BackendResult<ByteArray> = notScripted
    var updateUserResult: BackendResult<User> = notScripted
    var putUserPhotoResult: BackendResult<User> = notScripted
    var deleteUserPhotoResult: BackendResult<User> = notScripted
    var listSubjectsResult: BackendResult<SubjectsListResponse> = notScripted
    var createSubjectResult: BackendResult<Subject> = notScripted
    var listTopicsResult: BackendResult<TopicsListResponse> = notScripted
    var createTopicResult: BackendResult<Topic> = notScripted
    var startSessionResult: BackendResult<Session> = notScripted
    var resumeSessionResult: BackendResult<Session> = notScripted
    var endSessionResult: BackendResult<SessionEndResponse> = notScripted
    var uploadCaptureResult: BackendResult<CaptureUploadResponse> = notScripted
    var listTopicCapturesResult: BackendResult<TopicCapturesResponse> = notScripted
    var captureThumbnailResult: BackendResult<ByteArray> = notScripted
    var addWebPageResult: BackendResult<WebPageAddResponse> = notScripted

    /** The [SessionEndRequest]s sent, oldest first. */
    val endSessionRequests: MutableList<SessionEndRequest> = mutableListOf()

    /** The last [WebPageAddRequest] sent, if any. */
    var lastWebPageRequest: WebPageAddRequest? = null
        private set

    /** The [UserUpdateRequest]s sent, oldest first. */
    val userUpdates: MutableList<UserUpdateRequest> = mutableListOf()

    /** The photos sent, oldest first: the bytes and their content type. */
    val userPhotoUploads: MutableList<Pair<ByteArray, String>> = mutableListOf()

    /** The calls made so far, oldest first. */
    val calls: MutableList<String> = mutableListOf()

    /** The last [PairRequest] sent, if any. */
    var lastPairRequest: PairRequest? = null
        private set

    /** The `userId` of the last authenticated call's credentials, if any. */
    var lastUserId: String? = null
        private set

    /** The `userId` of every authenticated call, oldest first (null: no user). */
    val userIds: MutableList<String?> = mutableListOf()

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

    override suspend fun listUsers(backend: BackendCredentials): BackendResult<UsersListResponse> =
        record("listUsers", backend) { listUsersResult }

    override suspend fun userPhoto(backend: BackendCredentials, photoUrl: String): BackendResult<ByteArray> =
        record("userPhoto", backend, photoUrl) { userPhotoResult }

    override suspend fun updateUser(
        backend: BackendCredentials,
        userId: String,
        request: UserUpdateRequest,
    ): BackendResult<User> {
        userUpdates += request
        return record("updateUser", backend, userId) { updateUserResult }
    }

    override suspend fun putUserPhoto(
        backend: BackendCredentials,
        userId: String,
        bytes: ByteArray,
        contentType: String,
    ): BackendResult<User> {
        userPhotoUploads += bytes to contentType
        return record("putUserPhoto", backend, userId) { putUserPhotoResult }
    }

    override suspend fun deleteUserPhoto(backend: BackendCredentials, userId: String): BackendResult<User> =
        record("deleteUserPhoto", backend, userId) { deleteUserPhotoResult }

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
    ): BackendResult<SessionEndResponse> {
        endSessionRequests += request
        return record("endSession", backend, sessionId) { endSessionResult }
    }

    override suspend fun uploadCapture(
        backend: BackendCredentials,
        sessionId: String,
        metadata: CaptureUploadRequest,
        images: List<CaptureImageBytes>,
    ): BackendResult<CaptureUploadResponse> =
        record("uploadCapture", backend, metadata.captureId) { uploadCaptureResult }

    override suspend fun addWebPage(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
        request: WebPageAddRequest,
    ): BackendResult<WebPageAddResponse> {
        lastWebPageRequest = request
        return record("addWebPage", backend, "$subjectId/$topicId") { addWebPageResult }
    }

    private fun <T> record(
        endpoint: String,
        backend: BackendCredentials,
        argument: String? = null,
        result: () -> BackendResult<T>,
    ): BackendResult<T> {
        calls += listOfNotNull(endpoint, backend.baseUrl, argument).joinToString(" ")
        lastToken = backend.token
        lastUserId = backend.userId
        userIds += backend.userId
        return result()
    }

    override suspend fun listTopicCaptures(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
    ): BackendResult<TopicCapturesResponse> =
        record("listTopicCaptures", backend, "$subjectId/$topicId") { listTopicCapturesResult }

    override suspend fun captureThumbnail(backend: BackendCredentials, thumbnailUrl: String): BackendResult<ByteArray> =
        record("captureThumbnail", backend, thumbnailUrl) { captureThumbnailResult }
}
