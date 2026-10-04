package com.titanarq.studentassistant.ui

import com.titanarq.studentassistant.backend.PairedBackends
import com.titanarq.studentassistant.protocol.User

/** The screens [com.titanarq.studentassistant.MainActivity] switches between. */
enum class Route {
    PAIRING,
    HOME,
    CAPTURE,
    BACKENDS,
    CONNECTION_TEST,

    /** A topic's notes and the editor chat, in a WebView of the backend's web UI (#83). */
    DESK,

    /** «Preguntar al tutor»: a topic's voice tutor (#248). */
    TUTOR,

    /** «¿Quién eres?»: which user of the active backend is using the app (#554). */
    USERS,

    /** «Editar perfil»: the selected user's name, email and photo (#555). */
    PROFILE,
}

/** The screens that act for the selected user: none of them is shown while no user is selected. */
val USER_SCOPED_ROUTES: Set<Route> = setOf(Route.HOME, Route.CAPTURE, Route.DESK, Route.TUTOR, Route.PROFILE)

/**
 * Where the app opens: pairing when no backend is stored yet, else «¿Quién eres?» while no user
 * is selected (every process start), else the subjects/topics home.
 */
fun startRoute(stored: PairedBackends, user: User?): Route = when {
    stored.backends.isEmpty() -> Route.PAIRING
    user == null -> Route.USERS
    else -> Route.HOME
}

/**
 * [route], or «¿Quién eres?» when it needs a user and a backend is stored but no user is selected
 * (after «Cerrar sesión», after switching the active backend, after the backend refused the user).
 * Pairing, the backends list and the connection test never need one.
 */
fun routeFor(route: Route, hasBackends: Boolean, user: User?): Route =
    if (hasBackends && user == null && route in USER_SCOPED_ROUTES) Route.USERS else route
