package com.titanarq.studentassistant.users

import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.protocol.USER_EMAIL_MAX_CHARS
import com.titanarq.studentassistant.protocol.USER_NAME_MAX_CHARS
import com.titanarq.studentassistant.ui.backendFailureMessage

@Composable
private fun fieldErrorMessage(error: ProfileFieldError): String = when (error) {
    ProfileFieldError.NAME_REQUIRED -> stringResource(R.string.profile_name_required)
    ProfileFieldError.NAME_TOO_LONG -> stringResource(R.string.profile_name_too_long, USER_NAME_MAX_CHARS)
    ProfileFieldError.EMAIL_INVALID -> stringResource(R.string.profile_email_invalid)
    ProfileFieldError.EMAIL_TOO_LONG -> stringResource(R.string.profile_email_too_long, USER_EMAIL_MAX_CHARS)
}

/** «Editar perfil»: name, email and photo of the selected user; saving returns [onDone] with «Perfil guardado». */
@Composable
fun ProfileScreen(
    viewModel: ProfileViewModel,
    photos: UserPhotos,
    onDone: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val user by viewModel.user.collectAsStateWithLifecycle()
    val context = LocalContext.current
    var confirmRemove by rememberSaveable { mutableStateOf(false) }
    val preparer = remember(context) { PhotoPreparer(context.contentResolver) }
    // The Photo Picker needs no storage permission; images only.
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null) viewModel.uploadPhoto { preparer.prepare(uri) }
    }
    val savedMessage = stringResource(R.string.profile_saved)
    LaunchedEffect(state.saved) {
        if (state.saved) {
            viewModel.onSavedShown()
            Toast.makeText(context, savedMessage, Toast.LENGTH_SHORT).show()
            onDone()
        }
    }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            modifier = Modifier.verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(stringResource(R.string.users_edit_profile), style = MaterialTheme.typography.headlineSmall)
            user?.let { current ->
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    UserAvatar(current, photos, size = 96.dp)
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(
                            onClick = {
                                picker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
                            },
                            enabled = !state.busy,
                        ) { Text(stringResource(R.string.profile_change_photo)) }
                        if (current.photoUrl != null) {
                            TextButton(onClick = { confirmRemove = true }, enabled = !state.busy) {
                                Text(stringResource(R.string.profile_remove_photo))
                            }
                        }
                    }
                }
            }
            OutlinedTextField(
                value = state.name,
                onValueChange = viewModel::onNameChange,
                label = { Text(stringResource(R.string.profile_name_label)) },
                isError = state.validation.name != null,
                supportingText = state.validation.name?.let { { Text(fieldErrorMessage(it)) } },
                singleLine = true,
                enabled = !state.busy,
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = state.email,
                onValueChange = viewModel::onEmailChange,
                label = { Text(stringResource(R.string.profile_email_label)) },
                isError = state.validation.email != null,
                supportingText = state.validation.email?.let { { Text(fieldErrorMessage(it)) } },
                singleLine = true,
                enabled = !state.busy,
                modifier = Modifier.fillMaxWidth(),
            )
            if (state.rejected) {
                Text(stringResource(R.string.profile_rejected), color = MaterialTheme.colorScheme.error)
            }
            if (state.photoUnreadable) {
                Text(stringResource(R.string.profile_photo_unreadable), color = MaterialTheme.colorScheme.error)
            }
            state.failure?.let { Text(backendFailureMessage(it), color = MaterialTheme.colorScheme.error) }
            if (state.busy) Text(stringResource(R.string.profile_saving))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = viewModel::save, enabled = !state.busy) { Text(stringResource(R.string.profile_save)) }
                TextButton(onClick = onDone, enabled = !state.busy) { Text(stringResource(R.string.cancel)) }
            }
        }
    }

    if (confirmRemove) {
        AlertDialog(
            onDismissRequest = { confirmRemove = false },
            title = { Text(stringResource(R.string.profile_remove_photo_confirm)) },
            confirmButton = {
                TextButton(onClick = {
                    confirmRemove = false
                    viewModel.removePhoto()
                }) { Text(stringResource(R.string.profile_remove_photo)) }
            },
            dismissButton = { TextButton(onClick = { confirmRemove = false }) { Text(stringResource(R.string.cancel)) } },
        )
    }
}
