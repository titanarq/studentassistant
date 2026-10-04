package com.titanarq.studentassistant.users

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.protocol.USER_EMAIL_MAX_CHARS
import com.titanarq.studentassistant.protocol.USER_EMAIL_PATTERN
import com.titanarq.studentassistant.protocol.USER_NAME_MAX_CHARS
import com.titanarq.studentassistant.protocol.User
import com.titanarq.studentassistant.protocol.UserUpdateRequest
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** Why a profile field is refused (the screen words it in Spanish). */
enum class ProfileFieldError { NAME_REQUIRED, NAME_TOO_LONG, EMAIL_INVALID, EMAIL_TOO_LONG }

/** The two fields' problems, null for a field that is fine. */
data class ProfileValidation(val name: ProfileFieldError?, val email: ProfileFieldError?) {
    val ok: Boolean get() = name == null && email == null
}

/**
 * The protocol's rules for the profile form: a required name of at most [USER_NAME_MAX_CHARS]
 * characters and an optional email (empty clears it) of at most [USER_EMAIL_MAX_CHARS] characters
 * that looks like an address. Both are checked as they would travel, i.e. trimmed.
 */
fun validateProfile(name: String, email: String): ProfileValidation {
    val n = name.trim()
    val e = email.trim()
    return ProfileValidation(
        name = when {
            n.isEmpty() -> ProfileFieldError.NAME_REQUIRED
            n.length > USER_NAME_MAX_CHARS -> ProfileFieldError.NAME_TOO_LONG
            else -> null
        },
        email = when {
            e.isEmpty() -> null
            e.length > USER_EMAIL_MAX_CHARS -> ProfileFieldError.EMAIL_TOO_LONG
            !USER_EMAIL_PATTERN.matches(e) -> ProfileFieldError.EMAIL_INVALID
            else -> null
        },
    )
}

/** What the profile screen shows. */
data class ProfileUiState(
    val name: String = "",
    val email: String = "",
    /** Problems of the fields, shown once «Guardar» was pressed and until the field changes. */
    val validation: ProfileValidation = ProfileValidation(null, null),
    /** A request is in flight (save, photo upload or removal). */
    val busy: Boolean = false,
    /** The backend refused the data (422). */
    val rejected: Boolean = false,
    /** The photo could not be read or is too big to send. */
    val photoUnreadable: Boolean = false,
    /** Another failed call, shown with the usual Spanish backend message. */
    val failure: BackendResult.Failure? = null,
    /** The profile was saved: the screen shows «Perfil guardado» and goes home. */
    val saved: Boolean = false,
)

/**
 * «Editar perfil» (#555): the selected user's name, email and photo. Nothing is decided here
 * (ADR-0001) beyond the protocol's field rules the form checks before sending; the backend answers
 * with the user after the change, which replaces the selection in [holder]. The user id is never
 * edited or shown.
 */
class ProfileViewModel(
    private val client: BackendClient,
    private val store: BackendStore,
    private val holder: UserHolder,
    private val photos: UserPhotos,
) : ViewModel() {
    private val initial = holder.current.value
    private val _state = MutableStateFlow(ProfileUiState(name = initial?.name.orEmpty(), email = initial?.email.orEmpty()))
    val state: StateFlow<ProfileUiState> = _state.asStateFlow()

    /** The user being edited, as last saved. */
    val user: StateFlow<User?> = holder.current

    fun onNameChange(value: String) = _state.update {
        it.copy(name = value, validation = it.validation.copy(name = null), rejected = false, failure = null)
    }

    fun onEmailChange(value: String) = _state.update {
        it.copy(email = value, validation = it.validation.copy(email = null), rejected = false, failure = null)
    }

    /** «Guardar»: validates, then sends only the fields that differ from the saved user. */
    fun save() {
        val current = holder.current.value ?: return
        val s = _state.value
        if (s.busy) return
        val validation = validateProfile(s.name, s.email)
        if (!validation.ok) {
            _state.update { it.copy(validation = validation) }
            return
        }
        val name = s.name.trim()
        val email = s.email.trim()
        val newName = name.takeIf { it != current.name }
        val newEmail = email.takeIf { it != current.email.orEmpty() }
        val request = if (newName == null && newEmail == null) null else UserUpdateRequest(newName, newEmail)
        if (request == null) {
            // Nothing changed: nothing to send, nothing to announce.
            _state.update { it.copy(saved = true) }
            return
        }
        send { backend -> client.updateUser(backend, current.id, request) }
    }

    /** Uploads the photo [prepare] produces (decoding and downscaling run in it, off the main thread). */
    fun uploadPhoto(prepare: suspend () -> PreparedPhoto?) {
        val current = holder.current.value ?: return
        if (_state.value.busy) return
        _state.update { it.copy(busy = true, photoUnreadable = false, rejected = false, failure = null) }
        viewModelScope.launch {
            val photo = prepare()
            if (photo == null || photo.bytes.size > PHOTO_MAX_BYTES) {
                _state.update { it.copy(busy = false, photoUnreadable = true) }
                return@launch
            }
            val active = store.active()
            if (active == null) {
                _state.update { it.copy(busy = false) }
                return@launch
            }
            finishPhoto(client.putUserPhoto(active.credentials, current.id, photo.bytes, photo.contentType))
        }
    }

    /** «Quitar foto», already confirmed. */
    fun removePhoto() {
        val current = holder.current.value ?: return
        if (_state.value.busy) return
        _state.update { it.copy(busy = true, photoUnreadable = false, rejected = false, failure = null) }
        viewModelScope.launch {
            val active = store.active()
            if (active == null) {
                _state.update { it.copy(busy = false) }
                return@launch
            }
            finishPhoto(client.deleteUserPhoto(active.credentials, current.id))
        }
    }

    /** The screen showed «Perfil guardado» and left. */
    fun onSavedShown() = _state.update { it.copy(saved = false) }

    private fun send(call: suspend (com.titanarq.studentassistant.backend.BackendCredentials) -> BackendResult<User>) {
        _state.update { it.copy(busy = true, rejected = false, failure = null, photoUnreadable = false) }
        viewModelScope.launch {
            val active = store.active()
            if (active == null) {
                _state.update { it.copy(busy = false) }
                return@launch
            }
            when (val result = call(active.credentials)) {
                is BackendResult.Success -> {
                    holder.select(result.value)
                    _state.update { it.copy(busy = false, saved = true) }
                }
                is BackendResult.Failure -> fail(result)
            }
        }
    }

    private fun finishPhoto(result: BackendResult<User>) {
        when (result) {
            is BackendResult.Success -> {
                photos.invalidate()
                holder.select(result.value)
                _state.update { it.copy(busy = false) }
            }
            is BackendResult.Failure -> fail(result)
        }
    }

    private fun fail(failure: BackendResult.Failure) {
        val rejected = failure is BackendResult.HttpError && failure.status == 422
        _state.update { it.copy(busy = false, rejected = rejected, failure = failure.takeUnless { rejected }) }
    }
}
