package com.titanarq.studentassistant.backend

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.users.UserHolder
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/** The paired-backends screen: lists the stored backends, switches the active one, removes one. */
class PairedBackendsViewModel(
    private val store: BackendStore,
    private val users: UserHolder = UserHolder(),
) : ViewModel() {
    /** Null until the store has been read once. */
    val backends: StateFlow<PairedBackends?> =
        store.backends.stateIn(viewModelScope, SharingStarted.Eagerly, null)

    /** Switches the active backend; the users of another vault are other people: «¿Quién eres?» is asked again. */
    fun setActive(deviceId: String) {
        viewModelScope.launch {
            if (store.current().activeDeviceId != deviceId) users.clear()
            store.setActive(deviceId)
        }
    }

    fun remove(deviceId: String) {
        viewModelScope.launch {
            if (store.current().activeDeviceId == deviceId) users.clear()
            store.remove(deviceId)
        }
    }
}
