package com.titanarq.studentassistant.tutor

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import okio.BufferedSource
import java.io.IOException

/** The tutor API's JSON: lenient, like the web UI's readers (unknown fields and bad values tolerated). */
internal val TutorJson = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    coerceInputValues = true
}

/** One Server-Sent Event: its `event:` name (`message` when none) and its `data:` lines joined. */
data class SseEvent(val event: String, val data: String)

/**
 * Reads Server-Sent Events line by line (the backend's `sse()`: `event:` + one `data:` line + a
 * blank line). Comment lines (`:`), `id:` and `retry:` are ignored; an event without data is
 * dropped, as the SSE spec says.
 */
class SseParser {
    private var event: String? = null
    private val data = StringBuilder()
    private var hasData = false

    /** Feeds one line (without its line ending); returns the event a blank line completes. */
    fun feed(line: String): SseEvent? {
        if (line.isEmpty()) {
            val complete = if (hasData) SseEvent(event ?: "message", data.toString()) else null
            event = null
            data.setLength(0)
            hasData = false
            return complete
        }
        if (line.startsWith(":")) return null
        val colon = line.indexOf(':')
        val field = if (colon < 0) line else line.substring(0, colon)
        val value = if (colon < 0) "" else line.substring(colon + 1).removePrefix(" ")
        when (field) {
            "event" -> event = value
            "data" -> {
                if (hasData) data.append('\n')
                data.append(value)
                hasData = true
            }
        }
        return null
    }
}

/**
 * Reads one question's answer stream from [source]: each `reply.delta` / `reply.restart` goes to
 * [onProgress]; the `result` is the answer, an `error` event a [TutorResult.Refused] with its own
 * status, and a stream that ends or breaks first is [TutorResult.Interrupted].
 */
fun readTutorStream(source: BufferedSource, onProgress: (TutorProgress) -> Unit): TutorResult<TutorAnswer> {
    val parser = SseParser()
    while (true) {
        val line = try {
            source.readUtf8Line()
        } catch (e: IOException) {
            return TutorResult.Interrupted
        } ?: return TutorResult.Interrupted
        val event = parser.feed(line) ?: continue
        when (event.event) {
            "reply.delta" -> {
                val text = (parseObject(event.data)?.get("text") as? JsonPrimitive)
                    ?.takeIf { it.isString }?.content
                if (!text.isNullOrEmpty()) onProgress(TutorProgress.Delta(text))
            }
            "reply.restart" -> onProgress(TutorProgress.Restart)
            "result" -> return try {
                TutorResult.Success(TutorJson.decodeFromString(TutorAnswer.serializer(), event.data))
            } catch (e: SerializationException) {
                TutorResult.InvalidResponse(e.message ?: "undecodable result")
            } catch (e: IllegalArgumentException) {
                TutorResult.InvalidResponse(e.message ?: "undecodable result")
            }
            "error" -> {
                val body = parseObject(event.data)
                val status = (body?.get("status") as? JsonPrimitive)?.intOrNull ?: HTTP_INTERNAL_ERROR
                return refusal(status, body)
            }
        }
    }
}

/** A refusal with [status], its `detail` and `code` read from an error [body] when present. */
internal fun refusal(status: Int, body: JsonObject?): TutorResult.Refused =
    TutorResult.Refused(status, body.stringField("detail"), body.stringField("code"))

/** [text] as a JSON object, or null when it is not one. */
internal fun parseObject(text: String): JsonObject? = try {
    TutorJson.parseToJsonElement(text) as? JsonObject
} catch (e: SerializationException) {
    null
} catch (e: IllegalArgumentException) {
    null
}

// FastAPI's validation errors put a list in `detail`: only a non-empty string counts.
private fun JsonObject?.stringField(name: String): String? =
    (this?.get(name) as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull?.takeIf { it.isNotBlank() }

private const val HTTP_INTERNAL_ERROR = 500
