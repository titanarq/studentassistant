package com.titanarq.studentassistant.desk

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.backend.BackendStore
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** Why the WebView's page did not load. */
sealed interface DeskLoadFailure {
    /** The backend refused the token (401): the phone must pair again. */
    data object Unauthorized : DeskLoadFailure

    /** The page never loaded (no network, backend down) or answered another error status. */
    data class Failed(val detail: String) : DeskLoadFailure
}

/** What the study desk screen shows. */
sealed interface DeskUiState {
    data object Loading : DeskUiState

    /** No active backend is stored: nothing to load. */
    data object NoBackend : DeskUiState

    /** The active backend's address is not an http(s) URL. */
    data object InvalidBackend : DeskUiState

    /**
     * The page to show; [reload] changes on every retry so the screen loads it again, and
     * [failure] is the last main-frame load's failure, cleared by a retry.
     */
    data class Ready(
        val backendName: String,
        val baseUrl: String,
        val page: DeskPage,
        val reload: Int = 0,
        val failure: DeskLoadFailure? = null,
    ) : DeskUiState
}

/**
 * The study desk screen (#83) for [topic] on the active backend: builds the [DeskPage] the WebView
 * loads and keeps the outcome of its loads. It never calls the backend itself: the web app does.
 */
class StudyDeskViewModel(
    private val store: BackendStore,
    val topic: DeskTopic,
) : ViewModel() {
    private val _state = MutableStateFlow<DeskUiState>(DeskUiState.Loading)
    val state: StateFlow<DeskUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            val backend = store.active()
            _state.value = when {
                backend == null -> DeskUiState.NoBackend
                else -> deskPage(backend.baseUrl, backend.token, topic)
                    ?.let { DeskUiState.Ready(backend.displayName, backend.baseUrl, it) }
                    ?: DeskUiState.InvalidBackend
            }
        }
    }

    /** The WebView's main frame answered [status] (>= 400), or failed with [detail] when [status] is null. */
    fun onLoadFailed(status: Int?, detail: String) {
        val failure = if (status == HTTP_UNAUTHORIZED) {
            DeskLoadFailure.Unauthorized
        } else {
            DeskLoadFailure.Failed(if (status != null) "HTTP $status" else detail)
        }
        _state.update { if (it is DeskUiState.Ready) it.copy(failure = failure) else it }
    }

    /** Loads the page again, clearing the last failure. */
    fun retry() {
        _state.update { if (it is DeskUiState.Ready) it.copy(reload = it.reload + 1, failure = null) else it }
    }

    private companion object {
        const val HTTP_UNAUTHORIZED = 401
    }
}
