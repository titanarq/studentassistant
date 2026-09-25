package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.protocol.CaptureTrigger

/**
 * Takes a burst of stills and uploads them (ADR-0001). The capture screen calls it on "Capturar"
 * ([CaptureTrigger.BUTTON]) and on a `capture_now` command ([CaptureTrigger.COMMAND] with its
 * `command_id`). The burst and the upload are android still capture's (#46); until then
 * [NoStillCapture] does nothing.
 */
fun interface StillCapture {
    fun capture(trigger: CaptureTrigger, commandId: String?)
}

/** The placeholder [StillCapture]: takes nothing. */
object NoStillCapture : StillCapture {
    override fun capture(trigger: CaptureTrigger, commandId: String?) = Unit
}
