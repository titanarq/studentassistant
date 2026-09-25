package com.titanarq.studentassistant.home

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
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
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.ui.backendFailureMessage
import java.text.DateFormat
import java.util.Date
import java.util.Locale

/** Subjects -> topics of the active backend; "Nuevo tema"; start or continue a session. */
@Composable
fun HomeScreen(
    viewModel: HomeViewModel,
    onSessionOpened: () -> Unit,
    onBackends: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { viewModel.load() }
    LaunchedEffect(state.openedSession) {
        if (state.openedSession != null) {
            viewModel.onSessionShown()
            onSessionOpened()
        }
    }
    val subject = state.selectedSubject
    BackHandler(enabled = subject != null) { viewModel.clearSubject() }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    subject?.name ?: stringResource(R.string.home_title),
                    style = MaterialTheme.typography.headlineSmall,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = onBackends) { Text(stringResource(R.string.home_backends)) }
            }
            state.backendName?.let {
                Text(stringResource(R.string.home_backend, it), style = MaterialTheme.typography.bodySmall)
            }
            when {
                state.noBackend -> Text(stringResource(R.string.home_no_backend))
                subject == null -> SubjectList(state.subjects, onSelect = viewModel::selectSubject, onRetry = viewModel::load)
                else -> {
                    TextButton(onClick = viewModel::clearSubject) { Text(stringResource(R.string.back)) }
                    TopicList(
                        topics = state.topics ?: Loadable.Loading,
                        session = state.session,
                        onOpen = viewModel::startOrContinue,
                        onRetry = viewModel::refreshTopics,
                        modifier = Modifier.weight(1f, fill = false),
                    )
                }
            }
            if (!state.noBackend && state.subjects is Loadable.Loaded) {
                Button(onClick = viewModel::openCreateTopic) { Text(stringResource(R.string.home_new_topic)) }
            }
        }
    }

    state.createTopic?.let { dialog ->
        CreateTopicDialog(
            dialog = dialog,
            subjects = (state.subjects as? Loadable.Loaded)?.value.orEmpty(),
            initialSubject = subject?.name.orEmpty(),
            onCreate = viewModel::createTopic,
            onDismiss = viewModel::dismissCreateTopic,
        )
    }

    (state.session as? SessionAction.Failed)?.let { failed ->
        AlertDialog(
            onDismissRequest = viewModel::dismissSessionFailure,
            text = { Text(sessionFailureMessage(failed.failure)) },
            confirmButton = {
                TextButton(onClick = viewModel::dismissSessionFailure) { Text(stringResource(R.string.home_dismiss)) }
            },
        )
    }
}

@Composable
private fun SubjectList(
    subjects: Loadable<List<Subject>>,
    onSelect: (Subject) -> Unit,
    onRetry: () -> Unit,
) {
    when (subjects) {
        Loadable.Loading -> Text(stringResource(R.string.loading))
        is Loadable.Failed -> LoadFailure(subjects.failure, onRetry)
        is Loadable.Loaded ->
            if (subjects.value.isEmpty()) {
                Text(stringResource(R.string.home_no_subjects))
            } else {
                LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(subjects.value, key = { it.subjectId }) { subject ->
                        Card(modifier = Modifier.fillMaxWidth().clickable { onSelect(subject) }) {
                            Text(
                                subject.name,
                                style = MaterialTheme.typography.titleMedium,
                                modifier = Modifier.padding(16.dp),
                            )
                        }
                    }
                }
            }
    }
}

@Composable
private fun TopicList(
    topics: Loadable<List<TopicRow>>,
    session: SessionAction,
    onOpen: (TopicRow) -> Unit,
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
                    items(topics.value, key = { it.topic.topicId }) { row ->
                        TopicCard(
                            row = row,
                            opening = (session as? SessionAction.Opening)?.topicId == row.topic.topicId,
                            busy = session is SessionAction.Opening,
                            onOpen = { onOpen(row) },
                        )
                    }
                }
            }
    }
}

@Composable
private fun TopicCard(row: TopicRow, opening: Boolean, busy: Boolean, onOpen: () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(row.topic.name, style = MaterialTheme.typography.titleMedium)
            if (row.ending) {
                Text(
                    stringResource(R.string.home_session_ending),
                    color = MaterialTheme.colorScheme.secondary,
                    style = MaterialTheme.typography.bodySmall,
                )
            } else if (row.canContinue) {
                Text(
                    stringResource(R.string.home_session_open),
                    color = MaterialTheme.colorScheme.primary,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            row.lastSessionAtMs?.let {
                Text(stringResource(R.string.home_last_session, formatDate(it)), style = MaterialTheme.typography.bodySmall)
            }
            row.pendingCount?.takeIf { it > 0 }?.let {
                Text(pluralStringResource(R.plurals.home_pending_doubts, it, it), style = MaterialTheme.typography.bodySmall)
            }
            val label = when {
                opening -> R.string.home_opening_session
                row.canContinue -> R.string.home_continue_session
                else -> R.string.home_start_session
            }
            Button(onClick = onOpen, enabled = !busy) { Text(stringResource(label)) }
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

@Composable
private fun CreateTopicDialog(
    dialog: CreateTopicDialog,
    subjects: List<Subject>,
    initialSubject: String,
    onCreate: (String, String) -> Unit,
    onDismiss: () -> Unit,
) {
    var subjectName by rememberSaveable { mutableStateOf(initialSubject) }
    var title by rememberSaveable { mutableStateOf("") }
    val isNewSubject = subjectName.isNotBlank() &&
        subjects.none { it.name.trim().equals(subjectName.trim(), ignoreCase = true) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.create_topic_title)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = subjectName,
                    onValueChange = { subjectName = it },
                    label = { Text(stringResource(R.string.create_topic_subject)) },
                    placeholder = { Text(stringResource(R.string.create_topic_subject_hint)) },
                    singleLine = true,
                    enabled = !dialog.saving,
                )
                if (subjects.isNotEmpty()) {
                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        subjects.take(MAX_SUBJECT_CHIPS).forEach { subject ->
                            TextButton(onClick = { subjectName = subject.name }, enabled = !dialog.saving) {
                                Text(subject.name, maxLines = 1)
                            }
                        }
                    }
                }
                if (isNewSubject) {
                    Text(
                        stringResource(R.string.create_topic_new_subject, subjectName.trim()),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                OutlinedTextField(
                    value = title,
                    onValueChange = { if (it.length <= MAX_NAME_LENGTH) title = it },
                    label = { Text(stringResource(R.string.create_topic_name)) },
                    singleLine = true,
                    enabled = !dialog.saving,
                )
                dialog.failure?.let { Text(backendFailureMessage(it), color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onCreate(subjectName, title) },
                enabled = !dialog.saving && subjectName.isNotBlank() && title.isNotBlank(),
            ) {
                Text(stringResource(if (dialog.saving) R.string.create_topic_saving else R.string.create_topic_save))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !dialog.saving) { Text(stringResource(R.string.cancel)) }
        },
    )
}

@Composable
private fun sessionFailureMessage(failure: SessionFailure): String = when (failure) {
    SessionFailure.Conflict -> stringResource(R.string.home_session_conflict)
    is SessionFailure.Backend -> backendFailureMessage(failure.failure)
}

private fun formatDate(epochMs: Long): String =
    DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT, Locale.forLanguageTag("es")).format(Date(epochMs))

/** The protocol's `name` limit for subjects and topics. */
private const val MAX_NAME_LENGTH = 200

/** How many existing subjects the dialog offers as one-tap choices. */
private const val MAX_SUBJECT_CHIPS = 3
