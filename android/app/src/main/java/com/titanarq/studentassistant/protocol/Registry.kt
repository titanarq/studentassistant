package com.titanarq.studentassistant.protocol

import kotlin.reflect.KClass
import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.serializer

/**
 * How one named message (`protocol/<name>.schema.json`) is decoded and encoded. Events go through
 * their sealed hierarchy, so decoding checks the wire `type` exactly as a live socket would.
 */
class MessageCodec internal constructor(
    /** The class a valid message of this name decodes to. */
    val messageClass: KClass<*>,
    private val serializer: KSerializer<Any>,
) {
    fun decode(element: JsonElement): Any = ProtocolJson.decodeFromJsonElement(serializer, element)

    fun encode(message: Any): JsonElement {
        require(messageClass.isInstance(message)) {
            "expected ${messageClass.simpleName}, got ${message::class.simpleName}"
        }
        return ProtocolJson.encodeToJsonElement(serializer, message)
    }
}

@Suppress("UNCHECKED_CAST")
private inline fun <reified T : Any> rest(): MessageCodec =
    MessageCodec(T::class, serializer<T>() as KSerializer<Any>)

@Suppress("UNCHECKED_CAST")
private inline fun <reified T : ClientEvent> client(): MessageCodec =
    MessageCodec(T::class, ClientEvent.serializer() as KSerializer<Any>)

@Suppress("UNCHECKED_CAST")
private inline fun <reified T : ServerEvent> server(): MessageCodec =
    MessageCodec(T::class, ServerEvent.serializer() as KSerializer<Any>)

/** Maps each `protocol/<name>.schema.json` name to its codec; mirrors the backend's `MODELS`. */
val MESSAGE_CODECS: Map<String, MessageCodec> = mapOf(
    "client.hello" to client<Hello>(),
    "client.transcript.client.partial" to client<TranscriptClientPartial>(),
    "client.transcript.client.final" to client<TranscriptClientFinal>(),
    "client.button" to client<Button>(),
    "client.marker" to client<Marker>(),
    "client.ack" to client<ClientAck>(),
    "server.hello.ack" to server<HelloAck>(),
    "server.transcript.partial" to server<TranscriptPartial>(),
    "server.transcript.final" to server<TranscriptFinal>(),
    "server.command" to server<Command>(),
    "server.notice" to server<Notice>(),
    "server.ack" to server<ServerAck>(),
    "rest.pair.request" to rest<PairRequest>(),
    "rest.pair.response" to rest<PairResponse>(),
    "rest.health.response" to rest<HealthResponse>(),
    "rest.subjects.list.response" to rest<SubjectsListResponse>(),
    "rest.subjects.create.request" to rest<SubjectCreateRequest>(),
    "rest.subjects.create.response" to rest<Subject>(),
    "rest.topics.list.response" to rest<TopicsListResponse>(),
    "rest.topics.create.request" to rest<TopicCreateRequest>(),
    "rest.topics.create.response" to rest<Topic>(),
    "rest.sessions.start.request" to rest<SessionStartRequest>(),
    "rest.sessions.start.response" to rest<Session>(),
    "rest.sessions.resume.response" to rest<Session>(),
    "rest.sessions.end.request" to rest<SessionEndRequest>(),
    "rest.sessions.end.response" to rest<SessionEndResponse>(),
    "rest.sessions.captures.request" to rest<CaptureUploadRequest>(),
    "rest.sessions.captures.response" to rest<CaptureUploadResponse>(),
    "rest.search.response" to rest<SearchResponse>(),
    "rest.topics.web_pages.create.request" to rest<WebPageAddRequest>(),
    "rest.topics.web_pages.create.response" to rest<WebPageAddResponse>(),
)

/** The codec for a message name such as `client.hello`; [NoSuchElementException] if none. */
fun codecFor(name: String): MessageCodec =
    MESSAGE_CODECS[name] ?: throw NoSuchElementException("no codec registered for protocol message '$name'")
