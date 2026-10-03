package com.titanarq.studentassistant.protocol

import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The REST error codes (protocol 1.2) and the two the users of 1.8 added (#545). */
class ErrorsTest {
    @Test
    fun `every code is read by the wire string the backend sends`() {
        assertEquals(
            listOf(
                "cost_cap_reached",
                "doubt_closed",
                "session_open",
                "notes_changed",
                "notes_busy",
                "user_required",
                "user_not_found",
            ),
            ErrorCode.entries.map { it.wire },
        )
        assertEquals(ErrorCode.entries.map { it.wire }, ErrorCode.entries.map { it.toString() })
    }

    @Test
    fun `every code carries the version that added it`() {
        assertEquals(ProtocolVersion(1, 2), ERROR_CODE_SINCE)
        assertEquals(ProtocolVersion(1, 8), USER_ERROR_CODES_SINCE)
        assertEquals(ErrorCode.entries.toSet(), ERROR_CODES_SINCE.keys)
        val addedIn18 = setOf(ErrorCode.USER_REQUIRED, ErrorCode.USER_NOT_FOUND)
        for (code in ErrorCode.entries) {
            assertEquals(code.wire, if (code in addedIn18) USER_ERROR_CODES_SINCE else ERROR_CODE_SINCE, ERROR_CODES_SINCE[code])
        }
    }

    @Test
    fun `a code this side does not know is not a code`() {
        assertTrue(isErrorCode("user_required"))
        assertFalse(isErrorCode("added_in_1_9"))
        assertFalse(isErrorCode("USER_REQUIRED"))
        assertFalse(isErrorCode(null))
    }

    @Test
    fun `an error body is read by its code, never by its Spanish detail`() {
        val required = buildJsonObject {
            put("detail", "¿Para quién es esto?")
            put("code", "user_required")
        }
        assertEquals(ErrorCode.USER_REQUIRED, errorCode(required))
        val missing = buildJsonObject {
            put("detail", "Aquí no hay nadie con ese nombre.")
            put("code", "user_not_found")
        }
        assertEquals(ErrorCode.USER_NOT_FOUND, errorCode(missing))
    }

    @Test
    fun `a body with no code this side knows has none`() {
        assertEquals(null, errorCode(null))
        assertEquals(null, errorCode(buildJsonObject { put("detail", "Algo ha ido mal.") }))
        assertEquals(null, errorCode(buildJsonObject { put("detail", "Algo ha ido mal."); put("code", "added_in_1_9") }))
        assertEquals(null, errorCode(buildJsonObject { put("code", 7) }))
        // A code of an older protocol is still no code: additive, never renumbered.
        assertEquals(ErrorCode.SESSION_OPEN, errorCode(buildJsonObject { put("code", "session_open") }))
    }
}
