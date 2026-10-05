package com.titanarq.studentassistant.ui

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

/**
 * The colours of the web design system (`web/src/styles/tokens.css`, #296/#299), as ARGB values:
 * the same names and hex values, light and dark (the OS setting). `DesignTokensTest` keeps them
 * equal to the CSS file, so the phone and the web app look alike.
 */
internal object DesignTokens {
    class Palette(
        val paper: Long,
        val surface: Long,
        val surfaceSunken: Long,
        val line: Long,
        val lineStrong: Long,
        val headerBg: Long,
        val onHeader: Long,
        val ink: Long,
        val inkMuted: Long,
        val accent: Long,
        val accentSoft: Long,
        val onAccent: Long,
        val correction: Long,
        val correctionSoft: Long,
        val onCorrection: Long,
        val highlight: Long,
        val onHighlight: Long,
    )

    val light = Palette(
        paper = 0xFFF8F8F5, surface = 0xFFFFFFFF, surfaceSunken = 0xFFEFEFEA, line = 0xFFDEDED7,
        lineStrong = 0xFFB4B6AD, headerBg = 0xFF253F80, onHeader = 0xFFFFFFFF, ink = 0xFF1D2230,
        inkMuted = 0xFF545A68, accent = 0xFF2A4C9E, accentSoft = 0xFFE5EBF7, onAccent = 0xFFFFFFFF,
        correction = 0xFFB3261E, correctionSoft = 0xFFFBE9E6, onCorrection = 0xFFFFFFFF,
        highlight = 0xFFFFF1A8, onHighlight = 0xFF3B3000,
    )

    val dark = Palette(
        paper = 0xFF17201D, surface = 0xFF1E2925, surfaceSunken = 0xFF131A18, line = 0xFF2F3C37,
        lineStrong = 0xFF4B5A54, headerBg = 0xFF213556, onHeader = 0xFFEEF1EA, ink = 0xFFE6E9E2,
        inkMuted = 0xFFA8B1AA, accent = 0xFF9FBCF4, accentSoft = 0xFF24324A, onAccent = 0xFF0F1829,
        correction = 0xFFF29B91, correctionSoft = 0xFF3A2421, onCorrection = 0xFF2A1210,
        // The dark highlight is translucent on the web; a solid mix over the dark paper here.
        highlight = 0xFF3D3A24, onHighlight = 0xFFF5E8B0,
    )
}

private fun Long.c() = Color(this)

internal fun lightScheme(): ColorScheme = DesignTokens.light.let { t ->
    lightColorScheme(
        primary = t.accent.c(), onPrimary = t.onAccent.c(),
        // The header band: the top bar uses primaryContainer / onPrimaryContainer.
        primaryContainer = t.headerBg.c(), onPrimaryContainer = t.onHeader.c(),
        secondary = t.accent.c(), onSecondary = t.onAccent.c(),
        secondaryContainer = t.accentSoft.c(), onSecondaryContainer = t.ink.c(),
        tertiary = t.onHighlight.c(), onTertiary = t.highlight.c(),
        tertiaryContainer = t.highlight.c(), onTertiaryContainer = t.onHighlight.c(),
        background = t.paper.c(), onBackground = t.ink.c(),
        surface = t.surface.c(), onSurface = t.ink.c(),
        surfaceVariant = t.surfaceSunken.c(), onSurfaceVariant = t.inkMuted.c(),
        surfaceContainer = t.surface.c(), surfaceContainerLow = t.paper.c(), surfaceContainerHigh = t.surfaceSunken.c(),
        outline = t.lineStrong.c(), outlineVariant = t.line.c(),
        error = t.correction.c(), onError = t.onCorrection.c(),
        errorContainer = t.correctionSoft.c(), onErrorContainer = t.correction.c(),
    )
}

internal fun darkScheme(): ColorScheme = DesignTokens.dark.let { t ->
    darkColorScheme(
        primary = t.accent.c(), onPrimary = t.onAccent.c(),
        primaryContainer = t.headerBg.c(), onPrimaryContainer = t.onHeader.c(),
        secondary = t.accent.c(), onSecondary = t.onAccent.c(),
        secondaryContainer = t.accentSoft.c(), onSecondaryContainer = t.ink.c(),
        tertiary = t.onHighlight.c(), onTertiary = t.highlight.c(),
        tertiaryContainer = t.highlight.c(), onTertiaryContainer = t.onHighlight.c(),
        background = t.paper.c(), onBackground = t.ink.c(),
        surface = t.surface.c(), onSurface = t.ink.c(),
        surfaceVariant = t.surfaceSunken.c(), onSurfaceVariant = t.inkMuted.c(),
        surfaceContainer = t.surface.c(), surfaceContainerLow = t.paper.c(), surfaceContainerHigh = t.surfaceSunken.c(),
        outline = t.lineStrong.c(), outlineVariant = t.line.c(),
        error = t.correction.c(), onError = t.onCorrection.c(),
        errorContainer = t.correctionSoft.c(), onErrorContainer = t.correction.c(),
    )
}

/** The app's Material 3 theme, its colours derived from the web design tokens (light and dark). */
@Composable
fun StudentAssistantTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val scheme = if (darkTheme) darkScheme() else lightScheme()
    val view = LocalView.current
    if (!view.isInEditMode) {
        // The header band reaches under the status bar: its icons are always light on it.
        SideEffect {
            view.context.findActivity()?.window?.let { window ->
                WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = false
            }
        }
    }
    MaterialTheme(colorScheme = scheme, content = content)
}

private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}
