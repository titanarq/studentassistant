package com.titanarq.studentassistant.spool

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The byte cap shared by every spool of the app (audio of every session plus pending captures).
 *
 * Each spool reports what it adds and removes; [nearCap] turns true at [warnFraction] of
 * [maxBytes] (the capture screen warns), and [overCap] tells the audio spool to drop its oldest
 * audio. Captures are never dropped to make room: when they alone pass the cap, the audio shrinks
 * to its newest segment and the warning stays on. Thread-safe.
 */
class SpoolBudget(val maxBytes: Long, private val warnFraction: Double = DEFAULT_WARN_FRACTION) {
    init {
        require(maxBytes > 0) { "maxBytes must be positive" }
        require(warnFraction in 0.0..1.0) { "warnFraction must be within 0..1" }
    }

    private var used = 0L
    private val _nearCap = MutableStateFlow(false)

    /** True while the spools hold at least [warnFraction] of [maxBytes]. */
    val nearCap: StateFlow<Boolean> = _nearCap.asStateFlow()

    /** Bytes held by every spool now. */
    val usedBytes: Long get() = synchronized(this) { used }

    /** True while the spools hold more than [maxBytes]. */
    val overCap: Boolean get() = synchronized(this) { used > maxBytes }

    /** A spool wrote ([delta] > 0) or deleted ([delta] < 0) bytes. */
    fun add(delta: Long) {
        synchronized(this) {
            used = (used + delta).coerceAtLeast(0)
            _nearCap.value = used >= (maxBytes * warnFraction).toLong()
        }
    }

    companion object {
        /** 512 MiB: ~4 h of 16 kHz PCM16 audio, or ~40 bursts of 3 full-resolution photos. */
        const val DEFAULT_MAX_BYTES: Long = 512L * 1024 * 1024

        const val DEFAULT_WARN_FRACTION: Double = 0.8
    }
}
