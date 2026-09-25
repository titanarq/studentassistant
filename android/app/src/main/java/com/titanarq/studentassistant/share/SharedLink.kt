package com.titanarq.studentassistant.share

/** Characters a shared text often glues to the end of a link that are not part of it. */
private const val TRAILING_PUNCTUATION = ".,;:!?]}>»\"'”’…"

private val HTTP_LINK = Regex("""https?://\S+""", RegexOption.IGNORE_CASE)

/** The protocol's longest `url` (`rest.topics.web_pages.create.request`). */
const val MAX_SHARED_URL_LENGTH = 2000

/**
 * The web address in what another app shared ("Compartir -> Student Assistant"): the first
 * http(s) link of [text] (`Intent.EXTRA_TEXT`, where browsers put the URL, sometimes after the
 * page title), else of [subject] (`Intent.EXTRA_SUBJECT`). Punctuation glued to its end is left
 * out, and a closing parenthesis too unless it closes one of the link (`.../Foo_(bar)`). Null when neither holds a link, or the link is longer than the backend accepts.
 */
fun extractSharedUrl(text: String?, subject: String? = null): String? {
    for (candidate in listOf(text, subject)) {
        val match = candidate?.let { HTTP_LINK.find(it) } ?: continue
        val url = trimTrailing(match.value)
        if (url.substringAfter("://").isNotEmpty() && url.length <= MAX_SHARED_URL_LENGTH) return url
    }
    return null
}

private fun trimTrailing(link: String): String {
    var end = link.length
    while (end > 0) {
        val last = link[end - 1]
        val unbalanced = last == ')' && link.take(end).count { it == ')' } > link.take(end).count { it == '(' }
        if (last !in TRAILING_PUNCTUATION && !unbalanced) break
        end--
    }
    return link.take(end)
}
