package com.titanarq.studentassistant.ui

import com.titanarq.studentassistant.backend.PairedBackends

/** The screens [com.titanarq.studentassistant.MainActivity] switches between. */
enum class Route {
    PAIRING,
    BACKENDS,
    CONNECTION_TEST,
}

/** Where the app opens: pairing when no backend is stored yet, else the paired backends. */
fun startRoute(stored: PairedBackends): Route =
    if (stored.backends.isEmpty()) Route.PAIRING else Route.BACKENDS
