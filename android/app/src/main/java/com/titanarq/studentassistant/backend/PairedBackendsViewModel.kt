package com.titanarq.studentassistant.backend

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/** The paired-backends screen: lists the stored backends, switches the active one, removes one. */
class PairedBackendsViewModel(private val store: BackendStore) : ViewModel() {
    /** Null until the store has been read once. */
    val backends: StateFlow<PairedBackends?> =
        store.backends.stateIn(viewModelScope, SharingStarted.Eagerly, null)

    fun setActive(deviceId: String) {
        viewModelScope.launch { store.setActive(deviceId) }
    }

    fun remove(deviceId: String) {
        viewModelScope.launch { store.remove(deviceId) }
    }
}
