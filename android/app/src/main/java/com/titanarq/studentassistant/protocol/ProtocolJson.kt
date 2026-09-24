package com.titanarq.studentassistant.protocol

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

/**
 * The JSON codec of the wire contract.
 *
 * - Unknown fields are a contract violation and fail decoding (`ignoreUnknownKeys = false`).
 * - Absent optional fields decode to `null`, and `null` fields are omitted when encoding
 *   (`explicitNulls = false`), so optional fields round-trip as "absent", never as `null`.
 * - Non-null defaults such as an empty `received_capture_ids` are always written
 *   (`encodeDefaults = true`).
 * - `ClientEvent` / `ServerEvent` are discriminated on the wire field `type`.
 */
val ProtocolJson: Json = Json {
    ignoreUnknownKeys = false
    explicitNulls = false
    encodeDefaults = true
    classDiscriminator = "type"
}

/**
 * Decodes one WebSocket text message the client sent. Throws [SerializationException] on an
 * unknown or missing `type` (never silently dropped) or on any other contract violation.
 */
fun decodeClientEvent(text: String): ClientEvent =
    ProtocolJson.decodeFromString(ClientEvent.serializer(), text)

/** Encodes a client event with its wire `type`. */
fun encodeClientEvent(event: ClientEvent): String =
    ProtocolJson.encodeToString(ClientEvent.serializer(), event)

/**
 * Decodes one WebSocket text message the backend sent. Throws [SerializationException] on an
 * unknown or missing `type` (never silently dropped) or on any other contract violation.
 */
fun decodeServerEvent(text: String): ServerEvent =
    ProtocolJson.decodeFromString(ServerEvent.serializer(), text)

/** Encodes a server event with its wire `type`. */
fun encodeServerEvent(event: ServerEvent): String =
    ProtocolJson.encodeToString(ServerEvent.serializer(), event)
