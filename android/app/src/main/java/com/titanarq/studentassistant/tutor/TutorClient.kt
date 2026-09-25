package com.titanarq.studentassistant.tutor

import com.titanarq.studentassistant.backend.BackendCredentials
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * The voice tutor API of the backend (`server/tutor_routes.py`, #82), as the phone uses it (#248):
 * `POST /api/subjects/{s}/topics/{t}/tutor` answers one question as a Server-Sent Events stream
 * (`reply.delta` ..., then `result` or `error`) and `GET .../tutor` lists the topic's earlier
 * questions. It is not part of the capture protocol (`protocol/`): like the web UI, bodies are read
 * leniently (unknown fields ignored).
 */

/** The topic the tutor screen asks about. */
data class TutorTopic(val subjectId: String, val topicId: String, val topicName: String)

/** One notes footnote an answer cites: `[label]` in the text, [text] in the list of sources. */
@Serializable
data class TutorRef(
    val label: String,
    val kind: String = "",
    val text: String = "",
    @SerialName("source_id") val sourceId: String? = null,
    val path: String? = null,
)

/** The backend's `TutorAnswer`: the `result` event of a question. */
@Serializable
data class TutorAnswer(
    val question: String,
    /** The answer, with the notes' `[^label]` marks (see [shownText] and [spokenText]). */
    val reply: String,
    val refs: List<TutorRef> = emptyList(),
    val warning: String? = null,
)

/** One earlier question and its answer (`TutorHistory.turns`); [time] is ISO 8601, empty when unknown. */
@Serializable
data class TutorTurn(
    val time: String = "",
    val question: String,
    val reply: String,
    val refs: List<TutorRef> = emptyList(),
    val warning: String? = null,
)

@Serializable
internal data class TutorHistory(val turns: List<TutorTurn> = emptyList())

@Serializable
internal data class TutorQuestion(
    val question: String,
    @SerialName("confirm_over_cap") val confirmOverCap: Boolean = false,
)

/** What the answer's stream says before its `result`. */
sealed interface TutorProgress {
    /** The next piece of the answer. */
    data class Delta(val text: String) : TutorProgress

    /** The answer starts over (`reply.restart`): what was streamed so far is dropped. */
    data object Restart : TutorProgress
}

/** The outcome of one tutor call. Failures are values, never exceptions. */
sealed interface TutorResult<out T> {
    data class Success<T>(val value: T) : TutorResult<T>

    sealed interface Failure : TutorResult<Nothing>

    /**
     * The backend refused, before the stream (an HTTP status) or inside it (an `error` event with
     * its status): [detail] is its Spanish explanation when it gave one, [code] its error code.
     */
    data class Refused(val status: Int, val detail: String?, val code: String? = null) : Failure {
        /** A reached cost cap: the same question may be asked again with `confirm_over_cap`. */
        val overCap: Boolean get() = code == COST_CAP_REACHED
    }

    /** No answer at all: bad address, connection refused, the phone is not on the LAN, ... */
    data class Unreachable(val reason: String) : Failure

    /** The stream ended (or broke) before its `result` or `error`. */
    data object Interrupted : Failure

    /** A 2xx body or a `result` that is not what the API promises. */
    data class InvalidResponse(val reason: String) : Failure

    companion object {
        /** The error code of a reached cost cap (protocol `ErrorCode.COST_CAP_REACHED`). */
        const val COST_CAP_REACHED = "cost_cap_reached"
    }
}

/** The tutor API; [OkHttpTutorClient] is the real one, tests use a scripted fake. */
interface TutorClient {
    /** `GET .../tutor`: the topic's questions so far, oldest first. */
    suspend fun history(backend: BackendCredentials, subjectId: String, topicId: String): TutorResult<List<TutorTurn>>

    /**
     * `POST .../tutor`: asks [question] and reads the answer's stream, reporting its progress to
     * [onProgress] (on any thread) until the `result`. [confirmOverCap] proceeds past a reached
     * cost cap. Cancelled with the calling coroutine.
     */
    suspend fun ask(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
        question: String,
        confirmOverCap: Boolean = false,
        onProgress: (TutorProgress) -> Unit = {},
    ): TutorResult<TutorAnswer>
}
