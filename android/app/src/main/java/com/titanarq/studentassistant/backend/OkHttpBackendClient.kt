package com.titanarq.studentassistant.backend

import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.CaptureUploadResponse
import com.titanarq.studentassistant.protocol.HealthResponse
import com.titanarq.studentassistant.protocol.PROTOCOL_VERSION
import com.titanarq.studentassistant.protocol.PairRequest
import com.titanarq.studentassistant.protocol.PairResponse
import com.titanarq.studentassistant.protocol.ProtocolJson
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
import com.titanarq.studentassistant.protocol.isCompatible
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()

/** The default HTTP client: LAN timeouts, no logging interceptor (the token is never logged). */
fun defaultOkHttpClient(): OkHttpClient =
    OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

/** How long [OkHttpBackendClient.addWebPage] waits for the answer: the backend fetches the page through Claude. */
const val WEB_PAGE_READ_TIMEOUT_SECONDS = 120L

/**
 * [BackendClient] over OkHttp, encoding and decoding the `protocol` classes with [ProtocolJson].
 * Calls are asynchronous (OkHttp's dispatcher) and cancelled with the calling coroutine.
 */
class OkHttpBackendClient(
    private val http: OkHttpClient = defaultOkHttpClient(),
    private val ourVersion: String = PROTOCOL_VERSION,
) : BackendClient {

    override suspend fun pair(baseUrl: String, request: PairRequest): BackendResult<PairResponse> =
        call(baseUrl, token = null, "api/pair", jsonBody(PairRequest.serializer(), request), PairResponse.serializer())
            .checkVersion { it.protocolVersion }

    override suspend fun health(baseUrl: String): BackendResult<HealthResponse> =
        call(baseUrl, token = null, "api/health", body = null, HealthResponse.serializer())
            .checkVersion { it.protocolVersion }

    override suspend fun listSubjects(backend: BackendCredentials): BackendResult<SubjectsListResponse> =
        call(backend, "api/subjects", body = null, SubjectsListResponse.serializer())

    override suspend fun createSubject(
        backend: BackendCredentials,
        request: SubjectCreateRequest,
    ): BackendResult<Subject> =
        call(backend, "api/subjects", jsonBody(SubjectCreateRequest.serializer(), request), Subject.serializer())

    override suspend fun listTopics(
        backend: BackendCredentials,
        subjectId: String,
    ): BackendResult<TopicsListResponse> =
        call(backend, listOf("api", "subjects", subjectId, "topics"), body = null, TopicsListResponse.serializer())

    override suspend fun createTopic(
        backend: BackendCredentials,
        subjectId: String,
        request: TopicCreateRequest,
    ): BackendResult<Topic> =
        call(
            backend,
            listOf("api", "subjects", subjectId, "topics"),
            jsonBody(TopicCreateRequest.serializer(), request),
            Topic.serializer(),
        )

    override suspend fun startSession(
        backend: BackendCredentials,
        request: SessionStartRequest,
    ): BackendResult<Session> =
        call(backend, "api/sessions", jsonBody(SessionStartRequest.serializer(), request), Session.serializer())
            .checkVersion { it.protocolVersion }

    override suspend fun resumeSession(backend: BackendCredentials, sessionId: String): BackendResult<Session> =
        call(
            backend,
            listOf("api", "sessions", sessionId, "resume"),
            ByteArray(0).toRequestBody(null),
            Session.serializer(),
        ).checkVersion { it.protocolVersion }

    override suspend fun endSession(
        backend: BackendCredentials,
        sessionId: String,
        request: SessionEndRequest,
    ): BackendResult<SessionEndResponse> =
        call(
            backend,
            listOf("api", "sessions", sessionId, "end"),
            jsonBody(SessionEndRequest.serializer(), request),
            SessionEndResponse.serializer(),
        )

    override suspend fun uploadCapture(
        backend: BackendCredentials,
        sessionId: String,
        metadata: CaptureUploadRequest,
        images: List<CaptureImageBytes>,
    ): BackendResult<CaptureUploadResponse> {
        require(images.size == metadata.images.size) {
            "uploadCapture: ${images.size} images for ${metadata.images.size} metadata entries"
        }
        val multipart = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                "metadata",
                null,
                ProtocolJson.encodeToString(CaptureUploadRequest.serializer(), metadata)
                    .toRequestBody(JSON_MEDIA_TYPE),
            )
        metadata.images.zip(images).forEach { (image, data) ->
            multipart.addFormDataPart(
                image.part,
                image.part,
                data.bytes.toRequestBody(image.contentType.toMediaType()),
            )
        }
        return call(
            backend,
            listOf("api", "sessions", sessionId, "captures"),
            multipart.build(),
            CaptureUploadResponse.serializer(),
        )
    }

    override suspend fun addWebPage(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
        request: WebPageAddRequest,
    ): BackendResult<WebPageAddResponse> =
        call(
            backend.baseUrl,
            backend.token,
            listOf("api", "subjects", subjectId, "topics", topicId, "web-pages"),
            jsonBody(WebPageAddRequest.serializer(), request),
            WebPageAddResponse.serializer(),
            http = slowHttp,
        )

    /** [http] with the longer read timeout of the calls the backend answers after asking Claude. */
    private val slowHttp: OkHttpClient by lazy {
        http.newBuilder().readTimeout(WEB_PAGE_READ_TIMEOUT_SECONDS, TimeUnit.SECONDS).build()
    }

    // --- plumbing ---

    private fun <T> jsonBody(serializer: KSerializer<T>, value: T): RequestBody =
        ProtocolJson.encodeToString(serializer, value).toRequestBody(JSON_MEDIA_TYPE)

    private suspend fun <T> call(
        backend: BackendCredentials,
        path: String,
        body: RequestBody?,
        serializer: KSerializer<T>,
    ): BackendResult<T> = call(backend, path.split('/'), body, serializer)

    private suspend fun <T> call(
        backend: BackendCredentials,
        segments: List<String>,
        body: RequestBody?,
        serializer: KSerializer<T>,
    ): BackendResult<T> = call(backend.baseUrl, backend.token, segments, body, serializer)

    private suspend fun <T> call(
        baseUrl: String,
        token: String?,
        path: String,
        body: RequestBody?,
        serializer: KSerializer<T>,
    ): BackendResult<T> = call(baseUrl, token, path.split('/'), body, serializer)

    /** `GET` when [body] is null, else `POST`; [token], when given, as the bearer. */
    private suspend fun <T> call(
        baseUrl: String,
        token: String?,
        segments: List<String>,
        body: RequestBody?,
        serializer: KSerializer<T>,
        http: OkHttpClient = this.http,
    ): BackendResult<T> {
        val url = buildUrl(baseUrl, segments)
            ?: return BackendResult.Unreachable("invalid backend URL")
        val request = Request.Builder()
            .url(url)
            .apply { if (token != null) header("Authorization", "Bearer $token") }
            .apply { if (body == null) get() else post(body) }
            .build()
        val response = when (val outcome = execute(http.newCall(request))) {
            is Outcome.Failed -> return BackendResult.Unreachable(outcome.reason)
            is Outcome.Answered -> outcome
        }
        if (response.status !in 200..299) return BackendResult.HttpError(response.status)
        return try {
            BackendResult.Success(ProtocolJson.decodeFromString(serializer, response.body))
        } catch (e: SerializationException) {
            BackendResult.InvalidResponse(e.message ?: "undecodable body")
        } catch (e: IllegalArgumentException) {
            BackendResult.InvalidResponse(e.message ?: "undecodable body")
        }
    }

    private fun <T> BackendResult<T>.checkVersion(version: (T) -> String): BackendResult<T> {
        if (this !is BackendResult.Success) return this
        val peer = version(value)
        return try {
            if (isCompatible(peer, ourVersion)) this else BackendResult.IncompatibleVersion(peer, ourVersion)
        } catch (e: IllegalArgumentException) {
            BackendResult.InvalidResponse(e.message ?: "malformed protocol_version")
        }
    }

    private sealed interface Outcome {
        class Answered(val status: Int, val body: String) : Outcome
        class Failed(val reason: String) : Outcome
    }

    /** Runs [call] on OkHttp's dispatcher, reading the whole body; cancelled with the coroutine. */
    private suspend fun execute(call: Call): Outcome = suspendCancellableCoroutine { continuation ->
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(
            object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    // The reason names the exception only: never the request, which carries the token.
                    continuation.resume(Outcome.Failed(e.javaClass.simpleName + (e.message?.let { ": $it" } ?: "")))
                }

                override fun onResponse(call: Call, response: Response) {
                    val outcome = try {
                        response.use { Outcome.Answered(it.code, it.body?.string().orEmpty()) }
                    } catch (e: IOException) {
                        Outcome.Failed(e.javaClass.simpleName + (e.message?.let { ": $it" } ?: ""))
                    }
                    continuation.resume(outcome)
                }
            },
        )
    }

    companion object {
        /** `baseUrl` + path segments (each percent-encoded), or null when `baseUrl` is not HTTP(S). */
        fun buildUrl(baseUrl: String, segments: List<String>): HttpUrl? {
            val base = baseUrl.toHttpUrlOrNull() ?: return null
            val builder = base.newBuilder()
            // Drop the empty trailing segment of `http://host:port/` so paths never start with `//`.
            if (base.pathSegments.lastOrNull() == "") builder.removePathSegment(base.pathSegments.size - 1)
            segments.forEach { builder.addPathSegment(it) }
            return builder.build()
        }
    }
}
