package com.titanarq.studentassistant.ui

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assume.assumeTrue
import org.junit.Test

/** The Android palette is the web design system's (`web/src/styles/tokens.css`), light and dark. */
class DesignTokensTest {
    private val css: String? = generateSequence(File("").absoluteFile) { it.parentFile }
        .map { File(it, "web/src/styles/tokens.css") }
        .firstOrNull { it.isFile }
        ?.readText()
        ?.replace(Regex("/\\*[\\s\\S]*?\\*/"), "")

    private fun block(source: String, afterDark: Boolean): Map<String, String> {
        val from = if (afterDark) source.indexOf("prefers-color-scheme: dark") else 0
        val body = Regex(":root\\s*\\{([^{}]*)\\}").find(source, from)!!.groupValues[1]
        return body.split(";").mapNotNull { decl ->
            val colon = decl.indexOf(':')
            val name = decl.substring(0, maxOf(colon, 0)).trim()
            if (colon < 0 || !name.startsWith("--")) null else name to decl.substring(colon + 1).trim()
        }.toMap()
    }

    private fun hex(value: String?): Long = 0xFF000000 or value!!.removePrefix("#").toLong(16)

    private fun check(tokens: Map<String, String>, palette: DesignTokens.Palette) {
        assertEquals(hex(tokens["--paper"]), palette.paper)
        assertEquals(hex(tokens["--surface"]), palette.surface)
        assertEquals(hex(tokens["--surface-sunken"]), palette.surfaceSunken)
        assertEquals(hex(tokens["--line"]), palette.line)
        assertEquals(hex(tokens["--line-strong"]), palette.lineStrong)
        assertEquals(hex(tokens["--header-bg"]), palette.headerBg)
        assertEquals(hex(tokens["--on-header"]), palette.onHeader)
        assertEquals(hex(tokens["--ink"]), palette.ink)
        assertEquals(hex(tokens["--ink-muted"]), palette.inkMuted)
        assertEquals(hex(tokens["--accent"]), palette.accent)
        assertEquals(hex(tokens["--accent-soft"]), palette.accentSoft)
        assertEquals(hex(tokens["--on-accent"]), palette.onAccent)
        assertEquals(hex(tokens["--correction"]), palette.correction)
        assertEquals(hex(tokens["--correction-soft"]), palette.correctionSoft)
        assertEquals(hex(tokens["--on-correction"]), palette.onCorrection)
        assertEquals(hex(tokens["--on-highlight"]), palette.onHighlight)
    }

    @Test
    fun `the light palette equals the web tokens`() {
        val source = css
        assumeTrue("web tokens not in the checkout", source != null)
        val light = block(source!!, afterDark = false)
        check(light, DesignTokens.light)
        assertEquals(hex(light["--highlight"]), DesignTokens.light.highlight)
    }

    @Test
    fun `the dark palette equals the web tokens`() {
        val source = css
        assumeTrue("web tokens not in the checkout", source != null)
        // Dark overrides on top of light, as in the browser.
        check(block(source!!, afterDark = false) + block(source, afterDark = true), DesignTokens.dark)
    }
}
