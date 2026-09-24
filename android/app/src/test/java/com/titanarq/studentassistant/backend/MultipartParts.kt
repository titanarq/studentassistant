package com.titanarq.studentassistant.backend

/** A minimal multipart/form-data reader for asserting what the client uploaded. */
data class MultipartPart(val name: String, val contentType: String?, val body: ByteArray)

object MultipartParts {
    fun parse(body: ByteArray, boundary: String): List<MultipartPart> {
        val text = String(body, Charsets.ISO_8859_1) // byte-preserving
        return text.split("--$boundary")
            .drop(1)
            .filterNot { it.startsWith("--") }
            .map { raw ->
                val part = raw.removePrefix("\r\n").removeSuffix("\r\n")
                val headers = part.substringBefore("\r\n\r\n").lines()
                val content = part.substringAfter("\r\n\r\n")
                val disposition = headers.first { it.startsWith("Content-Disposition", ignoreCase = true) }
                val name = Regex("name=\"([^\"]*)\"").find(disposition)!!.groupValues[1]
                val type = headers.firstOrNull { it.startsWith("Content-Type", ignoreCase = true) }
                    ?.substringAfter(':')?.trim()
                MultipartPart(name, type, content.toByteArray(Charsets.ISO_8859_1))
            }
    }
}
