package com.titanarq.studentassistant.users

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.protocol.User
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** What «¿Quién eres?» shows. */
sealed interface UsersUiState {
    data object Loading : UsersUiState

    /** No backend is stored: nothing to ask. */
    data object NoBackend : UsersUiState

    /** The active backend's users; an empty list means the vault has none. */
    data class Loaded(val users: List<User>) : UsersUiState

    /** The list could not be read; [failure] is shown with «Reintentar». */
    data class Failed(val failure: BackendResult.Failure) : UsersUiState
}

/**
 * «¿Quién eres?»: the active backend's users (`GET /api/users`) and the choice of one, which is
 * kept in [holder] (memory only). Nothing about the users is decided here (ADR-0001): the backend
 * lists them and accepts or refuses the id the app then sends.
 */
class UsersViewModel(
    private val client: BackendClient,
    private val store: BackendStore,
    private val holder: UserHolder,
) : ViewModel() {
    private val _state = MutableStateFlow<UsersUiState>(UsersUiState.Loading)
    val state: StateFlow<UsersUiState> = _state.asStateFlow()

    private var job: Job? = null

    /** Reads the active backend's users (also «Reintentar»). */
    fun load() {
        job?.cancel()
        _state.value = UsersUiState.Loading
        job = viewModelScope.launch {
            val active = store.active()
            _state.value = if (active == null) {
                UsersUiState.NoBackend
            } else {
                when (val result = client.listUsers(active.credentials)) {
                    is BackendResult.Success -> UsersUiState.Loaded(result.value.users)
                    is BackendResult.Failure -> UsersUiState.Failed(result)
                }
            }
        }
    }

    /** The student is [user]: from now on every call acts for them. */
    fun select(user: User) {
        holder.select(user)
    }
}
