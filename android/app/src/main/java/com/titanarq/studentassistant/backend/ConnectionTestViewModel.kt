package com.titanarq.studentassistant.backend

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.users.UserHolder
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** The outcome of one check of the connection test. */
sealed interface CheckState {
    data object NotRun : CheckState

    data object Running : CheckState

    /** [detail]: the backend's protocol version (health) or the number of subjects (subjects). */
    data class Passed(val detail: String) : CheckState

    data class Failed(val failure: BackendResult.Failure) : CheckState
}

data class ConnectionTestUiState(
    /** The active backend's display name; null when no backend is stored. */
    val backendName: String? = null,
    /** True once the store was read and holds no active backend. */
    val noBackend: Boolean = false,
    /** `GET /api/health` (unauthenticated). */
    val health: CheckState = CheckState.NotRun,
    /**
     * The authenticated check with the stored token: `GET /api/subjects` as the selected user, or,
     * while nobody is selected ([asUser] false), `GET /api/users`, which needs no user.
     */
    val access: CheckState = CheckState.NotRun,
    /** True when [access] ran as the selected user (subjects), false when it listed the users. */
    val asUser: Boolean = true,
) {
    val running: Boolean get() = health == CheckState.Running || access == CheckState.Running
}

/**
 * Tests the active backend: `GET /api/health`, then one authenticated call: `GET /api/subjects` as
 * the selected user or, while nobody is selected (right after pairing, a vault with several users
 * would answer 400 `user_required`), `GET /api/users`. Both always run, so the screen shows the
 * outcome of each.
 */
class ConnectionTestViewModel(
    private val client: BackendClient,
    private val store: BackendStore,
    private val users: UserHolder = UserHolder(),
) : ViewModel() {
    private val _state = MutableStateFlow(ConnectionTestUiState())
    val state: StateFlow<ConnectionTestUiState> = _state.asStateFlow()

    private var job: Job? = null

    /** Runs both checks on the active backend; a run already in flight is left alone. */
    fun run() {
        if (job?.isActive == true) return
        job = viewModelScope.launch {
            val backend = store.active()
            if (backend == null) {
                _state.value = ConnectionTestUiState(noBackend = true)
                return@launch
            }
            val userId = users.current.value?.id
            _state.value = ConnectionTestUiState(
                backendName = backend.displayName,
                health = CheckState.Running,
                access = CheckState.Running,
                asUser = userId != null,
            )
            val health = client.health(backend.baseUrl).toCheck { it.protocolVersion }
            _state.update { it.copy(health = health) }
            val access = if (userId != null) {
                client.listSubjects(backend.credentials.forUser(userId)).toCheck { it.subjects.size.toString() }
            } else {
                client.listUsers(backend.credentials).toCheck { it.users.size.toString() }
            }
            _state.update { it.copy(access = access) }
        }
    }

    private fun <T> BackendResult<T>.toCheck(detail: (T) -> String): CheckState = when (this) {
        is BackendResult.Success -> CheckState.Passed(detail(value))
        is BackendResult.Failure -> CheckState.Failed(this)
    }
}
