package com.titanarq.studentassistant.pairing

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.ui.pairingFailureMessage

/**
 * Pairing: the QR scanner on top and the manual form below, both through [PairingViewModel].
 * [onDone] leaves the screen once paired; [onBack], when given, shows a back button.
 */
@Composable
fun PairingScreen(
    viewModel: PairingViewModel,
    onDone: () -> Unit,
    onBack: (() -> Unit)?,
    modifier: Modifier = Modifier,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    var url by rememberSaveable { mutableStateOf("") }
    var code by rememberSaveable { mutableStateOf("") }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            modifier = Modifier.verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(stringResource(R.string.pairing_title), style = MaterialTheme.typography.headlineSmall)

            when (val current = state) {
                PairingUiState.Idle -> {
                    Text(stringResource(R.string.pairing_scan_hint), style = MaterialTheme.typography.bodyMedium)
                    QrScanner(
                        onQr = viewModel::onQrScanned,
                        modifier = Modifier.fillMaxWidth().aspectRatio(1f),
                    )
                }
                PairingUiState.Pairing -> {
                    CircularProgressIndicator()
                    Text(stringResource(R.string.pairing_in_progress))
                }
                is PairingUiState.Paired -> {
                    Text(stringResource(R.string.pairing_done, current.displayName))
                    Button(onClick = {
                        viewModel.reset()
                        onDone()
                    }) { Text(stringResource(R.string.pairing_continue)) }
                }
                is PairingUiState.Failed -> {
                    Text(
                        text = pairingFailureMessage(current.failure),
                        color = MaterialTheme.colorScheme.error,
                    )
                    Button(onClick = viewModel::reset) { Text(stringResource(R.string.pairing_retry)) }
                }
            }

            if (state !is PairingUiState.Paired) {
                Text(stringResource(R.string.manual_title), style = MaterialTheme.typography.titleMedium)
                OutlinedTextField(
                    value = url,
                    onValueChange = { url = it },
                    label = { Text(stringResource(R.string.manual_url_label)) },
                    placeholder = { Text(stringResource(R.string.manual_url_placeholder)) },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = code,
                    onValueChange = { code = it },
                    label = { Text(stringResource(R.string.manual_code_label)) },
                    placeholder = { Text(stringResource(R.string.manual_code_placeholder)) },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters),
                    modifier = Modifier.fillMaxWidth(),
                )
                Button(
                    onClick = { viewModel.onManualEntry(url, code) },
                    enabled = state !is PairingUiState.Pairing,
                ) { Text(stringResource(R.string.manual_submit)) }
            }

            if (onBack != null) {
                OutlinedButton(onClick = onBack) { Text(stringResource(R.string.back)) }
            }
        }
    }
}
