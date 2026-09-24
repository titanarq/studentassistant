package com.titanarq.studentassistant.backend

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R

/** The paired backends: which one is active, switch or remove one, pair another, test the link. */
@Composable
fun PairedBackendsScreen(
    viewModel: PairedBackendsViewModel,
    onPairNew: () -> Unit,
    onTestConnection: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val stored by viewModel.backends.collectAsStateWithLifecycle()
    var removing by rememberSaveable { mutableStateOf<String?>(null) }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(stringResource(R.string.backends_title), style = MaterialTheme.typography.headlineSmall)
            val current = stored
            when {
                current == null -> Text(stringResource(R.string.loading))
                current.backends.isEmpty() -> Text(stringResource(R.string.backends_empty))
                else -> LazyColumn(
                    modifier = Modifier.weight(1f, fill = false),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(current.backends, key = { it.deviceId }) { backend ->
                        BackendRow(
                            backend = backend,
                            active = backend.deviceId == current.activeDeviceId,
                            onMakeActive = { viewModel.setActive(backend.deviceId) },
                            onRemove = { removing = backend.deviceId },
                        )
                    }
                }
            }
            Button(onClick = onTestConnection, enabled = current?.active != null) {
                Text(stringResource(R.string.backends_test_connection))
            }
            OutlinedButton(onClick = onPairNew) { Text(stringResource(R.string.backends_pair_new)) }
        }
    }

    val target = stored?.backends?.firstOrNull { it.deviceId == removing }
    if (target != null) {
        AlertDialog(
            onDismissRequest = { removing = null },
            title = { Text(stringResource(R.string.backends_remove_confirm_title, target.displayName)) },
            text = { Text(stringResource(R.string.backends_remove_confirm_text)) },
            confirmButton = {
                TextButton(onClick = {
                    viewModel.remove(target.deviceId)
                    removing = null
                }) { Text(stringResource(R.string.backends_remove)) }
            },
            dismissButton = {
                TextButton(onClick = { removing = null }) { Text(stringResource(R.string.cancel)) }
            },
        )
    }
}

@Composable
private fun BackendRow(
    backend: PairedBackend,
    active: Boolean,
    onMakeActive: () -> Unit,
    onRemove: () -> Unit,
) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(backend.displayName, style = MaterialTheme.typography.titleMedium)
            Text(backend.baseUrl, style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (active) {
                    Text(
                        stringResource(R.string.backends_active),
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.padding(top = 12.dp),
                    )
                } else {
                    TextButton(onClick = onMakeActive) { Text(stringResource(R.string.backends_make_active)) }
                }
                TextButton(onClick = onRemove) { Text(stringResource(R.string.backends_remove)) }
            }
        }
    }
}
