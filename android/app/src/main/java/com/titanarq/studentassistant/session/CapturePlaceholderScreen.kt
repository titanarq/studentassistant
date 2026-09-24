package com.titanarq.studentassistant.session

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R

/**
 * Where "Empezar sesión" / "Continuar" lands: the open session from [SessionHolder]. A
 * placeholder until the capture screen (camera, transcript, buttons) replaces it.
 */
@Composable
fun CapturePlaceholderScreen(
    sessions: SessionHolder,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val open by sessions.current.collectAsStateWithLifecycle()
    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            open?.let {
                Text(
                    stringResource(R.string.capture_title, it.subjectName, it.topicName),
                    style = MaterialTheme.typography.headlineSmall,
                )
                Text(stringResource(R.string.capture_placeholder, it.session.sessionId))
            }
            OutlinedButton(onClick = onBack) { Text(stringResource(R.string.capture_back_home)) }
        }
    }
}
