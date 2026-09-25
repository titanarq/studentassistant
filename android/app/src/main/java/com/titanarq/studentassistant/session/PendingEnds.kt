package com.titanarq.studentassistant.session

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The session ends the student asked for that the backend has not taken yet, as the home screen
 * sees them (implemented by [com.titanarq.studentassistant.capture.SessionFinisher]).
 */
interface PendingEnds {
    /** Session ids whose end is being completed in the background («Terminando sesión…»). */
    val pending: StateFlow<Set<String>>

    /**
     * "Continuar" on [sessionId]: its pending end, if any, is stopped first (an attempt in flight is
     * cancelled and waited for, so no end request races [resume]), then [resume] runs. When it
     * returns true the student continues the session and the pending end is dropped for good;
     * otherwise (the resume failed) the pending end is taken up again. Returns what [resume]
     * returned.
     */
    suspend fun continueInstead(sessionId: String, resume: suspend () -> Boolean): Boolean
}

/** No pending ends: [continueInstead] just runs the resume. */
object NoPendingEnds : PendingEnds {
    private val none = MutableStateFlow<Set<String>>(emptySet()).asStateFlow()

    override val pending: StateFlow<Set<String>> get() = none

    override suspend fun continueInstead(sessionId: String, resume: suspend () -> Boolean): Boolean = resume()
}
