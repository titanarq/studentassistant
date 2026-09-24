package com.titanarq.studentassistant.pairing

import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * What the pairing QR carries: the backend's base URL and a one-time code. The backend encodes
 * it as the JSON `{"url", "code"}` (`studentassistant pair` and the web `/pair` page); the manual
 * form builds the same thing from what the student types.
 *
 * [url] is normalized to `scheme://host[:port][/path]` without a trailing slash; [code] is
 * trimmed and upper-cased (the backend ignores case, dashes and spaces anyway).
 */
data class PairingPayload(val url: String, val code: String) {
    override fun toString(): String = "PairingPayload(url=$url, code=<redacted>)"

    companion object {
        private val lenientJson = Json { ignoreUnknownKeys = true }

        @Serializable
        private data class Wire(val url: String, val code: String)

        /** Parses the text a QR decoded to. */
        fun parseQr(text: String): PairingPayloadResult {
            val wire = try {
                lenientJson.decodeFromString(Wire.serializer(), text.trim())
            } catch (e: SerializationException) {
                return PairingPayloadResult.Invalid(PairingPayloadError.NOT_A_PAIRING_QR)
            } catch (e: IllegalArgumentException) {
                return PairingPayloadResult.Invalid(PairingPayloadError.NOT_A_PAIRING_QR)
            }
            return of(wire.url, wire.code)
        }

        /**
         * Builds a payload from the manual form. A URL typed without a scheme
         * (`192.168.1.20:8000`) is taken as `http://`, since the backend serves plain HTTP on the LAN.
         */
        fun fromManualEntry(url: String, code: String): PairingPayloadResult {
            val trimmed = url.trim()
            val withScheme = if (trimmed.isEmpty() || "://" in trimmed) trimmed else "http://$trimmed"
            return of(withScheme, code)
        }

        private fun of(url: String, code: String): PairingPayloadResult {
            val normalizedUrl = normalizeUrl(url)
                ?: return PairingPayloadResult.Invalid(PairingPayloadError.INVALID_URL)
            val normalizedCode = code.trim().uppercase()
            if (normalizedCode.isEmpty()) return PairingPayloadResult.Invalid(PairingPayloadError.MISSING_CODE)
            return PairingPayloadResult.Valid(PairingPayload(normalizedUrl, normalizedCode))
        }

        /** `http(s)://host[:port][/path]` without query, fragment or trailing slash; else null. */
        fun normalizeUrl(url: String): String? {
            val trimmed = url.trim()
            val scheme = trimmed.substringBefore("://", missingDelimiterValue = "").lowercase()
            if (scheme != "http" && scheme != "https") return null
            val parsed = trimmed.toHttpUrlOrNull() ?: return null
            if (parsed.host.isEmpty() || parsed.query != null || parsed.fragment != null) return null
            if (parsed.username.isNotEmpty() || parsed.password.isNotEmpty()) return null
            return parsed.toString().trimEnd('/')
        }
    }
}

/** Why a QR or a manual entry is not a usable pairing payload. */
enum class PairingPayloadError {
    /** Not the JSON `{"url", "code"}` of a backend's pairing QR. */
    NOT_A_PAIRING_QR,

    /** The URL is missing, not HTTP(S), or carries a query, fragment or credentials. */
    INVALID_URL,

    /** The one-time code is empty. */
    MISSING_CODE,
}

sealed interface PairingPayloadResult {
    data class Valid(val payload: PairingPayload) : PairingPayloadResult

    data class Invalid(val error: PairingPayloadError) : PairingPayloadResult
}
