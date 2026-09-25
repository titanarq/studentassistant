package com.titanarq.studentassistant.desk

/**
 * The decisions behind the study desk WebView's file chooser and downloads (#259), kept free of
 * Android types so they are JVM-tested: the screen only wires them to `WebChromeClient`,
 * `DownloadListener` and `DownloadManager`.
 */

/** What the document picker offers when the page's `accept` names nothing it can map. */
const val ANY_MIME_TYPE = "*/*"

/** The file name used when the server's and the URL's give nothing usable. */
const val FALLBACK_FILE_NAME = "descarga"

/** File extensions an `accept` attribute may list, mapped to the MIME type the picker filters by. */
private val EXTENSION_MIME_TYPES = mapOf(
    ".pdf" to "application/pdf",
    ".png" to "image/png",
    ".jpg" to "image/jpeg",
    ".jpeg" to "image/jpeg",
    ".webp" to "image/webp",
    ".txt" to "text/plain",
    ".md" to "text/markdown",
    ".html" to "text/html",
    ".htm" to "text/html",
    ".csv" to "text/csv",
    ".json" to "application/json",
)

private val MIME_TYPE = Regex("^[a-z0-9!#$&^_.+-]+/([a-z0-9!#$&^_.+-]+|\\*)$")

/**
 * The MIME types the system document picker filters by for a file input whose `accept` is
 * [acceptTypes] (`FileChooserParams.getAcceptTypes()`: one entry per attribute value, each maybe
 * comma-separated). MIME types (`application/pdf`, an `image` wildcard) are kept, known extensions (`.pdf`)
 * are mapped, anything else is dropped; duplicates go, order stays. Nothing left means
 * [ANY_MIME_TYPE].
 */
fun acceptMimeTypes(acceptTypes: List<String?>): List<String> {
    val types = acceptTypes
        .asSequence()
        .filterNotNull()
        .flatMap { it.split(',') }
        .map { it.trim().lowercase() }
        .mapNotNull { entry ->
            when {
                entry.startsWith('.') -> EXTENSION_MIME_TYPES[entry]
                MIME_TYPE.matches(entry) -> entry
                else -> null
            }
        }
        .distinct()
        .toList()
    return types.ifEmpty { listOf(ANY_MIME_TYPE) }
}

/**
 * A download the page started, as the system download manager receives it: [url] on the paired
 * backend, [headers] with its bearer token, saved as [fileName] and shown with [mimeType] (null
 * lets the download manager decide). [headers] hold the token, so they never appear in [toString].
 */
data class DeskDownload(
    val url: String,
    val fileName: String,
    val mimeType: String?,
    val headers: Map<String, String>,
) {
    override fun toString(): String =
        "DeskDownload(url=$url, fileName=$fileName, mimeType=$mimeType, headers=${headers.keys})"
}

/** Whether a download the page started is handed to the system, or refused. */
sealed interface DownloadDecision {
    data class Start(val download: DeskDownload) : DownloadDecision

    /** [url] is not on the paired backend (another site, a `blob:` or `data:` URL): never fetched with the token. */
    data class Refused(val url: String) : DownloadDecision
}

/**
 * Decides what to do with a download the WebView reports ([url], its `Content-Disposition` and
 * MIME type) while showing the backend at [baseUrl] paired with [token]. Only the backend's own
 * origin gets the token; the file name comes from [guessFileName] (`URLUtil.guessFileName` on the
 * phone, reading `Content-Disposition` first) and is made safe by [safeFileName].
 */
fun decideDownload(
    url: String,
    contentDisposition: String?,
    mimeType: String?,
    baseUrl: String,
    token: String,
    guessFileName: (url: String, contentDisposition: String?, mimeType: String?) -> String?,
): DownloadDecision {
    if (!isSameOrigin(url, baseUrl)) return DownloadDecision.Refused(url)
    val type = mimeType?.trim()?.takeIf { it.isNotEmpty() }
    return DownloadDecision.Start(
        DeskDownload(
            url = url,
            fileName = safeFileName(guessFileName(url, contentDisposition, type)),
            mimeType = type,
            headers = mapOf("Authorization" to "Bearer $token"),
        ),
    )
}

private val UNSAFE_FILE_NAME_CHARS = Regex("[\\\\/:*?\"<>|\\p{Cntrl}]")

/**
 * [name] as a file name the Downloads folder accepts: no path separators, reserved or control
 * characters (each becomes `_`), no leading dots or surrounding blanks; [FALLBACK_FILE_NAME] when
 * nothing is left.
 */
fun safeFileName(name: String?): String {
    val cleaned = name.orEmpty()
        .replace(UNSAFE_FILE_NAME_CHARS, "_")
        .trim()
        .trimStart('.')
        .trim()
    return cleaned.ifEmpty { FALLBACK_FILE_NAME }
}
