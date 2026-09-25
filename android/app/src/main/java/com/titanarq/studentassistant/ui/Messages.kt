package com.titanarq.studentassistant.ui

import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.pairing.PairingFailure
import com.titanarq.studentassistant.pairing.PairingPayloadError
import com.titanarq.studentassistant.tutor.TutorResult

/** The Spanish message the student sees for a failed backend call. */
@Composable
fun backendFailureMessage(failure: BackendResult.Failure): String = when (failure) {
    is BackendResult.HttpError ->
        if (failure.status == 401) {
            stringResource(R.string.error_unauthorized)
        } else {
            stringResource(R.string.error_http, failure.status)
        }
    is BackendResult.Unreachable -> stringResource(R.string.error_unreachable)
    is BackendResult.IncompatibleVersion ->
        stringResource(R.string.error_incompatible_version, failure.peer, failure.ours)
    is BackendResult.InvalidResponse -> stringResource(R.string.error_invalid_response)
}

/** The Spanish message the student sees when pairing fails. */
@Composable
fun pairingFailureMessage(failure: PairingFailure): String = when (failure) {
    is PairingFailure.InvalidPayload -> when (failure.error) {
        PairingPayloadError.NOT_A_PAIRING_QR -> stringResource(R.string.error_not_a_pairing_qr)
        PairingPayloadError.INVALID_URL -> stringResource(R.string.error_invalid_url)
        PairingPayloadError.MISSING_CODE -> stringResource(R.string.error_missing_code)
    }
    PairingFailure.CodeRejected -> stringResource(R.string.error_code_rejected)
    is PairingFailure.HttpError -> stringResource(R.string.error_http, failure.status)
    PairingFailure.Unreachable -> stringResource(R.string.error_unreachable)
    is PairingFailure.IncompatibleVersion ->
        stringResource(R.string.error_incompatible_version, failure.peer, failure.ours)
    PairingFailure.InvalidResponse -> stringResource(R.string.error_invalid_response)
}

/**
 * The Spanish message the student sees when the tutor gives no answer (#248): the backend's own
 * Spanish `detail` when it gave one (busy, no notes yet, cost cap, Claude failed, ...).
 */
@Composable
fun tutorFailureMessage(failure: TutorResult.Failure): String = when (failure) {
    is TutorResult.Refused -> when {
        failure.status == 401 -> stringResource(R.string.error_unauthorized)
        failure.detail != null -> failure.detail
        failure.status == 404 -> stringResource(R.string.tutor_error_not_found)
        failure.status == 503 -> stringResource(R.string.tutor_error_unavailable)
        else -> stringResource(R.string.error_http, failure.status)
    }
    is TutorResult.Unreachable -> stringResource(R.string.error_unreachable)
    TutorResult.Interrupted -> stringResource(R.string.tutor_error_interrupted)
    is TutorResult.InvalidResponse -> stringResource(R.string.error_invalid_response)
}
