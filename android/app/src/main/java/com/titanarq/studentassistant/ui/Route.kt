package com.titanarq.studentassistant.ui

import com.titanarq.studentassistant.backend.PairedBackends

/** The screens [com.titanarq.studentassistant.MainActivity] switches between. */
enum class Route {
    PAIRING,
    HOME,
    CAPTURE,
    BACKENDS,
    CONNECTION_TEST,

    /** A topic's notes and the editor chat, in a WebView of the backend's web UI (#83). */
    DESK,
}

/** Where the app opens: pairing when no backend is stored yet, else the subjects/topics home. */
fun startRoute(stored: PairedBackends): Route =
    if (stored.backends.isEmpty()) Route.PAIRING else Route.HOME
