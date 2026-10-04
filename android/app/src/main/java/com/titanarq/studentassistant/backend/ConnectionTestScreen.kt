package com.titanarq.studentassistant.backend

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.ui.backendFailureMessage

/** Runs the connection test on the active backend when opened and shows each check's outcome. */
@Composable
fun ConnectionTestScreen(
    viewModel: ConnectionTestViewModel,
    modifier: Modifier = Modifier,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    LaunchedEffect(viewModel) { viewModel.run() }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            if (state.noBackend) {
                Text(stringResource(R.string.connection_no_backend))
            } else {
                state.backendName?.let { Text(stringResource(R.string.connection_backend, it)) }
                CheckRow(stringResource(R.string.connection_health), state.health) {
                    stringResource(R.string.connection_health_ok, it)
                }
                if (state.asUser) {
                    CheckRow(stringResource(R.string.connection_subjects), state.access) {
                        stringResource(R.string.connection_subjects_ok, it)
                    }
                } else {
                    CheckRow(stringResource(R.string.connection_users), state.access) {
                        stringResource(R.string.connection_users_ok, it)
                    }
                }
                Button(onClick = viewModel::run, enabled = !state.running) {
                    Text(stringResource(R.string.connection_run_again))
                }
            }
        }
    }
}

@Composable
private fun CheckRow(label: String, check: CheckState, passed: @Composable (String) -> String) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(label, style = MaterialTheme.typography.titleMedium)
        val (text, color) = when (check) {
            CheckState.NotRun -> stringResource(R.string.connection_not_run) to Color.Unspecified
            CheckState.Running -> stringResource(R.string.connection_running) to Color.Unspecified
            is CheckState.Passed -> passed(check.detail) to MaterialTheme.colorScheme.primary
            is CheckState.Failed -> backendFailureMessage(check.failure) to MaterialTheme.colorScheme.error
        }
        Text(text, color = color, style = MaterialTheme.typography.bodyMedium)
    }
}
