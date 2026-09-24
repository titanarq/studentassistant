package com.titanarq.studentassistant.session

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.protocol.Session
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * A session the backend opened for this phone (started or resumed), with the names the capture
 * screen shows and the backend it lives on.
 */
data class OpenSession(
    val backend: BackendCredentials,
    val session: Session,
    val subjectName: String,
    val topicName: String,
)

/**
 * The session the capture screen works on, handed over by the home screen. In memory only: after
 * a process restart the home screen lists the topic with its `open_session_id` and "Continuar"
 * resumes it.
 */
class SessionHolder {
    private val _current = MutableStateFlow<OpenSession?>(null)

    /** The open session, or null before one is started or resumed. */
    val current: StateFlow<OpenSession?> = _current.asStateFlow()

    fun open(session: OpenSession) {
        _current.value = session
    }

    fun clear() {
        _current.value = null
    }
}
