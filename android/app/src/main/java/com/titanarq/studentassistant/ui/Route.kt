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

    /** «¿Quién eres?»: which user of the active backend is using the app (#554). */
    USERS,

    /** «Editar perfil»: the selected user's name, email and photo (#555). */
    PROFILE,
}

/** The screens that act for the selected user: none of them is shown while no user is selected. */
val USER_SCOPED_ROUTES: Set<Route> = setOf(Route.HOME, Route.CAPTURE, Route.PROFILE)

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

/**
 * Where the top bar's Back icon (and the system back) of [route] goes, or null when the route has no
 * Back: the initial screens («¿Quién eres?», or pairing when nothing is paired yet). The settings
 * (computers) screen returns to [settingsFrom], the screen its gear was tapped on. Home and capture
 * handle their own back (subject list / ending the session), so they are not decided here.
 */
fun backRoute(route: Route, hasBackends: Boolean, settingsFrom: Route): Route? = when (route) {
    Route.PAIRING -> if (hasBackends) Route.BACKENDS else null
    Route.BACKENDS -> settingsFrom
    Route.CONNECTION_TEST -> Route.BACKENDS
    Route.PROFILE -> Route.HOME
    Route.USERS, Route.HOME, Route.CAPTURE -> null
}
