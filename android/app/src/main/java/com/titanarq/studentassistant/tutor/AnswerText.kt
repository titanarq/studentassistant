package com.titanarq.studentassistant.tutor

/**
 * The tutor's answer as it is shown and as it is read aloud (the web's `tutor/speech.ts`, #82):
 * the notes' `[^label]` footnote marks become `[label]` on screen, matching the list of sources,
 * and are left out when spoken, with the notes' `[[?..]]` doubts and Markdown symbols.
 */

private val FOOTNOTE_REF = Regex("""\[\^[^\]\s]+\](?!:)""")
private val UNCERTAIN = Regex("""\[\[\?([^\]]*)\]\]""")
private val EMPHASIS = Regex("""(\*\*|__|\*|_|`|\$)""")
private val HEADING = Regex("""^#{1,6}\s+""", RegexOption.MULTILINE)
private val SPACE_BEFORE_PUNCTUATION = Regex("""\s+([.,;:!?])""")
private val SPACES = Regex("""\s+""")

/** The answer as it is shown: each `[^label]` as `[label]`. */
fun shownText(reply: String): String = FOOTNOTE_REF.replace(reply) { "[" + it.value.substring(2, it.value.length - 1) + "]" }

/** The answer as it is read aloud: no `[^label]` marks, `[[?..]]` doubts or Markdown symbols. */
fun spokenText(reply: String): String = reply
    .replace(FOOTNOTE_REF, "")
    .replace(UNCERTAIN, "$1")
    .replace(HEADING, "")
    .replace(EMPHASIS, "")
    .replace(SPACE_BEFORE_PUNCTUATION, "$1")
    .replace(SPACES, " ")
    .trim()

/**
 * [text] cut into pieces of at most [maxLength] characters for a synthesizer with an input limit
 * (Android's `TextToSpeech.getMaxSpeechInputLength()`, 4000): at the last sentence end, else the
 * last space, of each window; a word longer than the window is cut where it must be.
 */
fun speechChunks(text: String, maxLength: Int): List<String> {
    require(maxLength > 0) { "maxLength must be positive" }
    val chunks = mutableListOf<String>()
    var rest = text.trim()
    while (rest.length > maxLength) {
        val window = rest.substring(0, maxLength + 1)
        val sentenceEnd = window.lastIndexOfAny(charArrayOf('.', '!', '?', ';', ':')).takeIf { it in 1 until maxLength }
        val cut = sentenceEnd?.plus(1) ?: window.lastIndexOf(' ').takeIf { it > 0 } ?: maxLength
        chunks += rest.substring(0, cut).trim()
        rest = rest.substring(cut).trim()
    }
    if (rest.isNotEmpty()) chunks += rest
    return chunks.filter { it.isNotEmpty() }
}
