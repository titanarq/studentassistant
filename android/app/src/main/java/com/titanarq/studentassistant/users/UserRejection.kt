package com.titanarq.studentassistant.users

import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
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
import com.titanarq.studentassistant.protocol.UsersListResponse
import com.titanarq.studentassistant.protocol.WebPageAddRequest
import com.titanarq.studentassistant.protocol.WebPageAddResponse
import com.titanarq.studentassistant.tutor.TutorAnswer
import com.titanarq.studentassistant.tutor.TutorClient
import com.titanarq.studentassistant.tutor.TutorProgress
import com.titanarq.studentassistant.tutor.TutorResult
import com.titanarq.studentassistant.tutor.TutorTurn
import com.titanarq.studentassistant.protocol.ErrorCode

// A `400 user_required` or `404 user_not_found` from any call means the backend no longer accepts
// the selected user: the selection is dropped and the app asks who is using it again (#554). The
// two decorators below apply that rule to every call without each screen repeating it. Only a call
// made as the selected user clears it: an old spooled item sent as another user, or a call with no
// user, says nothing about the selection.

/** The tutor's wire code of the user refusals (the tutor result carries it as a string). */
private fun TutorResult.Refused.userRejected(): Boolean =
    (status == 400 && code == ErrorCode.USER_REQUIRED.wire) ||
        (status == 404 && code == ErrorCode.USER_NOT_FOUND.wire)

/** A [BackendClient] that clears [users] when a user-scoped call is refused for its user. */
class UserRejectionBackendClient(
    private val delegate: BackendClient,
    private val users: UserHolder,
) : BackendClient {
    private fun <T> BackendResult<T>.checked(backend: BackendCredentials): BackendResult<T> {
        val userId = backend.userId
        if (userId != null && this is BackendResult.HttpError && userRejected) users.clearIfSelected(userId)
        return this
    }

    override suspend fun pair(baseUrl: String, request: PairRequest): BackendResult<PairResponse> =
        delegate.pair(baseUrl, request)

    override suspend fun health(baseUrl: String): BackendResult<HealthResponse> = delegate.health(baseUrl)

    override suspend fun listUsers(backend: BackendCredentials): BackendResult<UsersListResponse> =
        delegate.listUsers(backend)

    override suspend fun userPhoto(backend: BackendCredentials, photoUrl: String): BackendResult<ByteArray> =
        delegate.userPhoto(backend, photoUrl)

    override suspend fun listSubjects(backend: BackendCredentials): BackendResult<SubjectsListResponse> =
        delegate.listSubjects(backend).checked(backend)

    override suspend fun createSubject(
        backend: BackendCredentials,
        request: SubjectCreateRequest,
    ): BackendResult<Subject> = delegate.createSubject(backend, request).checked(backend)

    override suspend fun listTopics(
        backend: BackendCredentials,
        subjectId: String,
    ): BackendResult<TopicsListResponse> = delegate.listTopics(backend, subjectId).checked(backend)

    override suspend fun createTopic(
        backend: BackendCredentials,
        subjectId: String,
        request: TopicCreateRequest,
    ): BackendResult<Topic> = delegate.createTopic(backend, subjectId, request).checked(backend)

    override suspend fun startSession(
        backend: BackendCredentials,
        request: SessionStartRequest,
    ): BackendResult<Session> = delegate.startSession(backend, request).checked(backend)

    override suspend fun resumeSession(backend: BackendCredentials, sessionId: String): BackendResult<Session> =
        delegate.resumeSession(backend, sessionId).checked(backend)

    override suspend fun endSession(
        backend: BackendCredentials,
        sessionId: String,
        request: SessionEndRequest,
    ): BackendResult<SessionEndResponse> = delegate.endSession(backend, sessionId, request).checked(backend)

    override suspend fun uploadCapture(
        backend: BackendCredentials,
        sessionId: String,
        metadata: CaptureUploadRequest,
        images: List<CaptureImageBytes>,
    ): BackendResult<CaptureUploadResponse> =
        delegate.uploadCapture(backend, sessionId, metadata, images).checked(backend)

    override suspend fun addWebPage(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
        request: WebPageAddRequest,
    ): BackendResult<WebPageAddResponse> =
        delegate.addWebPage(backend, subjectId, topicId, request).checked(backend)
}

/** A [TutorClient] that clears [users] when a tutor call is refused for its user. */
class UserRejectionTutorClient(
    private val delegate: TutorClient,
    private val users: UserHolder,
) : TutorClient {
    private fun <T> TutorResult<T>.checked(backend: BackendCredentials): TutorResult<T> {
        val userId = backend.userId
        if (userId != null && this is TutorResult.Refused && userRejected()) users.clearIfSelected(userId)
        return this
    }

    override suspend fun history(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
    ): TutorResult<List<TutorTurn>> = delegate.history(backend, subjectId, topicId).checked(backend)

    override suspend fun ask(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
        question: String,
        confirmOverCap: Boolean,
        onProgress: (TutorProgress) -> Unit,
    ): TutorResult<TutorAnswer> =
        delegate.ask(backend, subjectId, topicId, question, confirmOverCap, onProgress).checked(backend)
}
