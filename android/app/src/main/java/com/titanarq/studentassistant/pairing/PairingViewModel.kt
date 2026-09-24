package com.titanarq.studentassistant.pairing

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.PairedBackend
import com.titanarq.studentassistant.protocol.ClientKind
import com.titanarq.studentassistant.protocol.PROTOCOL_VERSION
import com.titanarq.studentassistant.protocol.PairRequest
import com.titanarq.studentassistant.protocol.isCompatible
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Why pairing did not happen; the pairing screen shows a Spanish message for each. */
sealed interface PairingFailure {
    /** The QR or the manual entry is not a usable `{url, code}`. */
    data class InvalidPayload(val error: PairingPayloadError) : PairingFailure

    /** 401: the code is wrong, expired or already used. */
    data object CodeRejected : PairingFailure

    /** Any other non-2xx answer. */
    data class HttpError(val status: Int) : PairingFailure

    /** The backend could not be reached at that URL. */
    data object Unreachable : PairingFailure

    /** The backend speaks another protocol MAJOR version. */
    data class IncompatibleVersion(val peer: String, val ours: String) : PairingFailure

    /** The backend answered something that is not protocol v1. */
    data object InvalidResponse : PairingFailure
}

sealed interface PairingUiState {
    /** Waiting for a QR or the manual form. */
    data object Idle : PairingUiState

    /** `POST /api/pair` in flight. */
    data object Pairing : PairingUiState

    /** Paired and stored as the active backend. */
    data class Paired(val displayName: String) : PairingUiState

    data class Failed(val failure: PairingFailure) : PairingUiState
}

/**
 * Pairs with a backend from a scanned QR or the manual form (both take the same path): calls
 * `POST /api/pair` as an Android client, refuses an incompatible MAJOR version, and stores the
 * backend as the active one.
 */
class PairingViewModel(
    private val client: BackendClient,
    private val store: BackendStore,
    private val deviceName: String,
    private val ourVersion: String = PROTOCOL_VERSION,
) : ViewModel() {
    private val _state = MutableStateFlow<PairingUiState>(PairingUiState.Idle)
    val state: StateFlow<PairingUiState> = _state.asStateFlow()

    /**
     * The text a QR decoded to. Taken only while [PairingUiState.Idle]: the analyzer keeps
     * reporting the same QR on every frame, and a rejected code must not be retried in a loop.
     */
    fun onQrScanned(text: String) {
        if (_state.value != PairingUiState.Idle) return
        submit(PairingPayload.parseQr(text))
    }

    /** The manual form's URL and code; also a retry after a failure. */
    fun onManualEntry(url: String, code: String) {
        val current = _state.value
        if (current is PairingUiState.Pairing || current is PairingUiState.Paired) return
        submit(PairingPayload.fromManualEntry(url, code))
    }

    /** Back to [PairingUiState.Idle] after a failure or a finished pairing, to pair again. */
    fun reset() {
        if (_state.value !is PairingUiState.Pairing) _state.value = PairingUiState.Idle
    }

    private fun submit(result: PairingPayloadResult) {
        when (result) {
            is PairingPayloadResult.Invalid ->
                _state.value = PairingUiState.Failed(PairingFailure.InvalidPayload(result.error))
            is PairingPayloadResult.Valid -> pair(result.payload)
        }
    }

    private fun pair(payload: PairingPayload) {
        _state.value = PairingUiState.Pairing
        viewModelScope.launch {
            val request = PairRequest(
                pairingCode = payload.code,
                deviceName = deviceName,
                clientKind = ClientKind.ANDROID,
                protocolVersion = ourVersion,
            )
            _state.value = when (val result = client.pair(payload.url, request)) {
                is BackendResult.Success -> {
                    val response = result.value
                    val compatible = try {
                        isCompatible(response.protocolVersion, ourVersion)
                    } catch (e: IllegalArgumentException) {
                        null
                    }
                    when (compatible) {
                        null -> PairingUiState.Failed(PairingFailure.InvalidResponse)
                        false -> PairingUiState.Failed(
                            PairingFailure.IncompatibleVersion(response.protocolVersion, ourVersion),
                        )
                        true -> {
                            val backend = PairedBackend(
                                baseUrl = payload.url,
                                deviceId = response.deviceId,
                                token = response.token,
                                displayName = displayNameOf(payload.url),
                            )
                            store.save(backend)
                            PairingUiState.Paired(backend.displayName)
                        }
                    }
                }
                is BackendResult.HttpError -> PairingUiState.Failed(
                    if (result.status == 401) PairingFailure.CodeRejected else PairingFailure.HttpError(result.status),
                )
                is BackendResult.Unreachable -> PairingUiState.Failed(PairingFailure.Unreachable)
                is BackendResult.IncompatibleVersion ->
                    PairingUiState.Failed(PairingFailure.IncompatibleVersion(result.peer, result.ours))
                is BackendResult.InvalidResponse -> PairingUiState.Failed(PairingFailure.InvalidResponse)
            }
        }
    }

    companion object {
        /** `host:port` of a normalized base URL (`host` alone on the scheme's default port). */
        fun displayNameOf(baseUrl: String): String {
            val url = baseUrl.toHttpUrlOrNull() ?: return baseUrl
            val defaultPort = if (url.isHttps) 443 else 80
            return if (url.port == defaultPort) url.host else "${url.host}:${url.port}"
        }
    }
}
