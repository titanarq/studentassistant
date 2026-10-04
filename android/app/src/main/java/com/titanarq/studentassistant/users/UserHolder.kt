package com.titanarq.studentassistant.users

import com.titanarq.studentassistant.protocol.User
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The user the student picked on «¿Quién eres?» (#554). In memory only, never written to DataStore
 * or any file: every process start, «Cerrar sesión» and a switch of the active backend leave it
 * empty, and the app then asks who is using it.
 */
class UserHolder {
    private val _current = MutableStateFlow<User?>(null)

    /** The selected user, or null before one is picked. */
    val current: StateFlow<User?> = _current.asStateFlow()

    fun select(user: User) {
        _current.value = user
    }

    fun clear() {
        _current.value = null
    }

    /** Clears the selection only when [userId] is still the selected user (a late answer for another one changes nothing). */
    fun clearIfSelected(userId: String) {
        _current.value = _current.value?.takeIf { it.id != userId }
    }
}
