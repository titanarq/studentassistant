package com.titanarq.studentassistant.tutor

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.OkHttpBackendClient
import com.titanarq.studentassistant.backend.defaultOkHttpClient
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.SerializationException
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

/**
 * How long the answer's stream may stay silent: Claude reads the notes and sources before the first
 * delta comes.
 */
const val TUTOR_READ_TIMEOUT_SECONDS = 180L

private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()

/**
 * [TutorClient] over OkHttp, with the paired token as `Authorization: Bearer`. Calls run on OkHttp's
 * dispatcher (the stream is read there too) and are cancelled with the calling coroutine. Nothing
 * is logged: requests carry the token.
 */
class OkHttpTutorClient(
    http: OkHttpClient = defaultOkHttpClient(),
) : TutorClient {
    private val http: OkHttpClient = http.newBuilder().readTimeout(TUTOR_READ_TIMEOUT_SECONDS, TimeUnit.SECONDS).build()

    override suspend fun history(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
    ): TutorResult<List<TutorTurn>> {
        val request = request(backend, subjectId, topicId)?.get()?.build()
            ?: return TutorResult.Unreachable("invalid backend URL")
        return execute(request) { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) return@execute refusalOf(response.code, body)
            try {
                TutorResult.Success(TutorJson.decodeFromString(TutorHistory.serializer(), body).turns)
            } catch (e: SerializationException) {
                TutorResult.InvalidResponse(e.message ?: "undecodable history")
            } catch (e: IllegalArgumentException) {
                TutorResult.InvalidResponse(e.message ?: "undecodable history")
            }
        }
    }

    override suspend fun ask(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
        question: String,
        confirmOverCap: Boolean,
        onProgress: (TutorProgress) -> Unit,
    ): TutorResult<TutorAnswer> {
        val json = TutorJson.encodeToString(TutorQuestion.serializer(), TutorQuestion(question, confirmOverCap))
        val request = request(backend, subjectId, topicId)
            ?.header("Accept", "text/event-stream")
            ?.post(json.toRequestBody(JSON_MEDIA_TYPE))
            ?.build()
            ?: return TutorResult.Unreachable("invalid backend URL")
        return execute(request) { response ->
            if (!response.isSuccessful) return@execute refusalOf(response.code, response.body?.string().orEmpty())
            val source = response.body?.source() ?: return@execute TutorResult.Interrupted
            readTutorStream(source, onProgress)
        }
    }

    private fun request(backend: BackendCredentials, subjectId: String, topicId: String): Request.Builder? {
        val url = OkHttpBackendClient.buildUrl(
            backend.baseUrl,
            listOf("api", "subjects", subjectId, "topics", topicId, "tutor"),
        ) ?: return null
        return Request.Builder().url(url).header("Authorization", "Bearer ${backend.token}")
    }

    private fun refusalOf(status: Int, body: String): TutorResult.Refused = refusal(status, parseObject(body))

    /** Runs [request] on OkHttp's dispatcher and hands its response to [read] there. */
    private suspend fun <T> execute(request: Request, read: (Response) -> TutorResult<T>): TutorResult<T> =
        suspendCancellableCoroutine { continuation ->
            val call = http.newCall(request)
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(
                object : Callback {
                    override fun onFailure(call: Call, e: IOException) {
                        // The reason names the exception only: never the request, which carries the token.
                        continuation.resume(TutorResult.Unreachable(e.javaClass.simpleName + (e.message?.let { ": $it" } ?: "")))
                    }

                    override fun onResponse(call: Call, response: Response) {
                        val outcome = try {
                            response.use(read)
                        } catch (e: IOException) {
                            TutorResult.Interrupted
                        }
                        continuation.resume(outcome)
                    }
                },
            )
        }
}
