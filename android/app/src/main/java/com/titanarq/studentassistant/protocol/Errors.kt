package com.titanarq.studentassistant.protocol

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive

// Machine-readable codes of REST error bodies (protocol 1.2, protocol/README.md "REST errors").
//
// A REST error body is `{"detail": "<Spanish sentence>", "code"?: "<code>"}`; a client branches on
// `code`, never on the wording of `detail`. Error bodies are read leniently: an unknown or missing
// code is `null`, which every caller treats as "no code". This app decoded no error body before
// 1.8, only the HTTP status; the codes now travel with the protocol so the users screens (#554,
// #555) can tell "say who you are" from "that user is not here" without matching Spanish.

/** The `code` of a REST error body a client branches on. */
enum class ErrorCode(val wire: String) {
    /** 409: the session's or the day's cost cap is reached; retrying with `confirm_over_cap` goes past it. */
    COST_CAP_REACHED("cost_cap_reached"),

    /** 409: the doubt was already answered, auto-resolved or dismissed. */
    DOUBT_CLOSED("doubt_closed"),

    /** 409: an unended session is in the way. */
    SESSION_OPEN("session_open"),

    /** 409: a student save (`PUT .../notes`) named a stale `base_revision`; the body also carries
     * the current notes' `text` and `revision`. */
    NOTES_CHANGED("notes_changed"),

    /** 409: "prepárame el tema", a restore or another rewrite holds the topic's notes. */
    NOTES_BUSY("notes_busy"),

    /** 400, since 1.8: the request does not say which user it acts for ([USER_HEADER] /
     * [USER_COOKIE]) and the vault holds more than one, so none can be assumed. */
    USER_REQUIRED("user_required"),

    /** 404, since 1.8: this vault has no user with the id the request named. */
    USER_NOT_FOUND("user_not_found"),
    ;

    override fun toString(): String = wire
}

private val ERROR_CODES_BY_WIRE: Map<String, ErrorCode> =
    ErrorCode.entries.associateBy { it.wire }

/**
 * The protocol version that added `code` to REST error bodies, and the version of every
 * [ErrorCode] except the two user ones.
 */
val ERROR_CODE_SINCE: ProtocolVersion = ProtocolVersion(1, 2)

/** The protocol version that added [ErrorCode.USER_REQUIRED] and [ErrorCode.USER_NOT_FOUND] (#545). */
val USER_ERROR_CODES_SINCE: ProtocolVersion = ProtocolVersion(1, 8)

/**
 * The protocol version each code was added in. Codes are additive: the backend sends one only to a
 * client that negotiated at least that version, so a client of an older one never sees the two user
 * codes -- and treats any code it does not know as none, as [errorCode] already does.
 */
val ERROR_CODES_SINCE: Map<ErrorCode, ProtocolVersion> = mapOf(
    ErrorCode.COST_CAP_REACHED to ERROR_CODE_SINCE,
    ErrorCode.DOUBT_CLOSED to ERROR_CODE_SINCE,
    ErrorCode.SESSION_OPEN to ERROR_CODE_SINCE,
    ErrorCode.NOTES_CHANGED to ERROR_CODE_SINCE,
    ErrorCode.NOTES_BUSY to ERROR_CODE_SINCE,
    ErrorCode.USER_REQUIRED to USER_ERROR_CODES_SINCE,
    ErrorCode.USER_NOT_FOUND to USER_ERROR_CODES_SINCE,
)

/** Whether [wire] is the `code` of an [ErrorCode] this side knows. */
fun isErrorCode(wire: String?): Boolean = wire != null && ERROR_CODES_BY_WIRE.containsKey(wire)

/** The `code` of an error [body], or `null` when it has none this side knows. */
fun errorCode(body: JsonObject?): ErrorCode? =
    body?.get("code")?.jsonPrimitive?.takeIf { it.isString }?.content?.let { ERROR_CODES_BY_WIRE[it] }
