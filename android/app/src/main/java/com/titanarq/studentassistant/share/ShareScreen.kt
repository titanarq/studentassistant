package com.titanarq.studentassistant.share

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.home.Loadable
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.Topic
import com.titanarq.studentassistant.ui.backendFailureMessage

/** "Guardar en un tema": pick the subject, then the topic the shared page is saved to. */
@Composable
fun ShareScreen(viewModel: ShareViewModel, onClose: () -> Unit, modifier: Modifier = Modifier) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { viewModel.load() }
    val subject = state.selectedSubject
    BackHandler(enabled = subject != null && state.saving == null) { viewModel.clearSubject() }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(stringResource(R.string.share_title), style = MaterialTheme.typography.headlineSmall)
            val url = state.url
            if (url == null) {
                Text(stringResource(R.string.share_no_link))
                Button(onClick = onClose) { Text(stringResource(R.string.share_close)) }
                return@Column
            }
            Text(url, style = MaterialTheme.typography.bodySmall, maxLines = 2, overflow = TextOverflow.Ellipsis)
            when (val outcome = state.outcome) {
                is ShareOutcome.Saved -> {
                    val message = if (outcome.alreadyKept) R.string.share_already_kept else R.string.share_saved
                    Text(stringResource(message, outcome.title, outcome.topic.name), color = MaterialTheme.colorScheme.primary)
                    Button(onClick = onClose) { Text(stringResource(R.string.share_close)) }
                    return@Column
                }
                is ShareOutcome.Failed ->
                    Text(shareFailureMessage(outcome.failure), color = MaterialTheme.colorScheme.error)
                null -> Unit
            }
            state.saving?.let { Text(stringResource(R.string.share_saving, it.name)) }
            when {
                state.noBackend -> Text(stringResource(R.string.share_no_backend))
                subject == null -> SubjectList(state.subjects, onSelect = viewModel::selectSubject, onRetry = viewModel::load)
                else -> {
                    Text(subject.name, style = MaterialTheme.typography.titleMedium)
                    TextButton(onClick = viewModel::clearSubject, enabled = state.saving == null) {
                        Text(stringResource(R.string.back))
                    }
                    TopicList(
                        topics = state.topics ?: Loadable.Loading,
                        busy = state.saving != null,
                        onPick = viewModel::save,
                        onRetry = { viewModel.selectSubject(subject) },
                        modifier = Modifier.weight(1f, fill = false),
                    )
                }
            }
            TextButton(onClick = onClose, enabled = state.saving == null) { Text(stringResource(R.string.cancel)) }
        }
    }
}

@Composable
private fun SubjectList(subjects: Loadable<List<Subject>>, onSelect: (Subject) -> Unit, onRetry: () -> Unit) {
    when (subjects) {
        Loadable.Loading -> Text(stringResource(R.string.loading))
        is Loadable.Failed -> LoadFailure(subjects.failure, onRetry)
        is Loadable.Loaded ->
            if (subjects.value.isEmpty()) {
                Text(stringResource(R.string.share_no_subjects))
            } else {
                LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(subjects.value, key = { it.subjectId }) { subject ->
                        Card(modifier = Modifier.fillMaxWidth().clickable { onSelect(subject) }) {
                            Text(subject.name, style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(16.dp))
                        }
                    }
                }
            }
    }
}

@Composable
private fun TopicList(
    topics: Loadable<List<Topic>>,
    busy: Boolean,
    onPick: (Topic) -> Unit,
    onRetry: () -> Unit,
    modifier: Modifier = Modifier,
) {
    when (topics) {
        Loadable.Loading -> Text(stringResource(R.string.loading))
        is Loadable.Failed -> LoadFailure(topics.failure, onRetry)
        is Loadable.Loaded ->
            if (topics.value.isEmpty()) {
                Text(stringResource(R.string.home_no_topics))
            } else {
                LazyColumn(modifier = modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(topics.value, key = { it.topicId }) { topic ->
                        Card(modifier = Modifier.fillMaxWidth()) {
                            Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                Text(topic.name, style = MaterialTheme.typography.titleMedium)
                                if (topic.openSessionId != null) {
                                    Text(
                                        stringResource(R.string.home_session_open),
                                        color = MaterialTheme.colorScheme.primary,
                                        style = MaterialTheme.typography.bodySmall,
                                    )
                                }
                                Button(onClick = { onPick(topic) }, enabled = !busy) {
                                    Text(stringResource(R.string.share_save_here))
                                }
                            }
                        }
                    }
                }
            }
    }
}

@Composable
private fun LoadFailure(failure: BackendResult.Failure, onRetry: () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(backendFailureMessage(failure), color = MaterialTheme.colorScheme.error)
        OutlinedButton(onClick = onRetry) { Text(stringResource(R.string.home_retry)) }
    }
}

/**
 * The Spanish message for a page that was not saved. The app reads no error body (protocol
 * README, "REST errors"), so the status says what went wrong.
 */
@Composable
private fun shareFailureMessage(failure: BackendResult.Failure): String =
    when ((failure as? BackendResult.HttpError)?.status) {
        404 -> stringResource(R.string.share_error_not_found)
        409 -> stringResource(R.string.share_error_cost_cap)
        422 -> stringResource(R.string.share_error_not_a_page)
        502 -> stringResource(R.string.share_error_claude)
        503 -> stringResource(R.string.share_error_unavailable)
        else -> backendFailureMessage(failure)
    }
