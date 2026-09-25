package com.titanarq.studentassistant.tutor

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.ui.tutorFailureMessage

/**
 * «Preguntar al tutor» for one topic (#248): the topic's earlier questions, then a question spoken
 * (one utterance, `es-ES`) or typed, its answer streamed with «Fuentes:» and read aloud. Leaving the
 * screen, or the app going to the background, stops listening and reading.
 */
@Composable
fun TutorScreen(viewModel: TutorViewModel, onBack: () -> Unit, modifier: Modifier = Modifier) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) viewModel.startVoice() else viewModel.onMicPermissionDenied()
    }
    val askByVoice = {
        val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) viewModel.startVoice() else permission.launch(Manifest.permission.RECORD_AUDIO)
    }
    val leave = {
        viewModel.onBackground()
        onBack()
    }
    BackHandler(onBack = leave)
    QuietInBackground(viewModel)

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                TextButton(onClick = leave) { Text(stringResource(R.string.back)) }
                Text(
                    stringResource(R.string.tutor_title, viewModel.topic.topicName),
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
            }
            when (state.setup) {
                TutorSetup.Loading -> Text(stringResource(R.string.loading))
                TutorSetup.NoBackend -> Text(stringResource(R.string.home_no_backend))
                is TutorSetup.Ready -> {
                    Conversation(state, viewModel, modifier = Modifier.weight(1f))
                    Controls(state, viewModel, onAskByVoice = askByVoice)
                }
            }
        }
    }
}

@Composable
private fun Conversation(state: TutorUiState, viewModel: TutorViewModel, modifier: Modifier = Modifier) {
    val list = rememberLazyListState()
    val pending = state.pending
    val itemCount = state.turns.size + (if (pending != null) 1 else 0) + (if (state.failure != null) 1 else 0) + 1
    LaunchedEffect(state.turns.size, pending?.question, pending?.partial?.length, state.failure) {
        list.animateScrollToItem(itemCount - 1)
    }
    LazyColumn(state = list, modifier = modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item {
            when (val history = state.history) {
                TutorHistoryState.Loading -> Text(stringResource(R.string.loading), style = MaterialTheme.typography.bodySmall)
                is TutorHistoryState.Failed -> Column {
                    Text(
                        stringResource(R.string.tutor_history_failed, tutorFailureMessage(history.failure)),
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodySmall,
                    )
                    TextButton(onClick = viewModel::retryHistory) { Text(stringResource(R.string.home_retry)) }
                }
                TutorHistoryState.Loaded -> if (state.turns.isEmpty() && pending == null) {
                    Text(stringResource(R.string.tutor_no_questions), style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
        items(state.turns) { turn ->
            TurnCard(turn, speechAvailable = state.speechAvailable, onReadAgain = { viewModel.readAgain(turn) })
        }
        if (pending != null) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Question(pending.question)
                        if (pending.partial.isEmpty()) {
                            Text(stringResource(R.string.tutor_thinking), fontStyle = FontStyle.Italic)
                        } else {
                            Text(shownText(pending.partial))
                        }
                    }
                }
            }
        }
        state.failure?.let { failure ->
            item { FailureCard(failure, canAsk = state.canAsk, viewModel = viewModel) }
        }
    }
}

@Composable
private fun Question(question: String) {
    Text(
        stringResource(R.string.tutor_you_asked, question),
        fontWeight = FontWeight.SemiBold,
        style = MaterialTheme.typography.bodyMedium,
    )
}

@Composable
private fun TurnCard(turn: TutorTurn, speechAvailable: Boolean, onReadAgain: () -> Unit) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Question(turn.question)
            Text(shownText(turn.reply))
            turn.warning?.let {
                Text(it, color = MaterialTheme.colorScheme.secondary, style = MaterialTheme.typography.bodySmall)
            }
            if (turn.refs.isNotEmpty()) {
                Text(stringResource(R.string.tutor_sources), fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.bodySmall)
                turn.refs.forEach { ref ->
                    Text(
                        stringResource(R.string.tutor_source, ref.label, ref.text),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            if (speechAvailable) {
                TextButton(onClick = onReadAgain) { Text(stringResource(R.string.tutor_read_again)) }
            }
        }
    }
}

@Composable
private fun FailureCard(failure: AskFailure, canAsk: Boolean, viewModel: TutorViewModel) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer),
    ) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Question(failure.question)
            Text(tutorFailureMessage(failure.failure), color = MaterialTheme.colorScheme.onErrorContainer)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (failure.overCap) {
                    Button(onClick = viewModel::confirmOverCap, enabled = canAsk) {
                        Text(stringResource(R.string.tutor_continue_over_cap))
                    }
                } else {
                    OutlinedButton(onClick = viewModel::retryFailed, enabled = canAsk) {
                        Text(stringResource(R.string.home_retry))
                    }
                }
                TextButton(onClick = viewModel::dismissFailure) { Text(stringResource(R.string.home_dismiss)) }
            }
        }
    }
}

@Composable
private fun Controls(state: TutorUiState, viewModel: TutorViewModel, onAskByVoice: () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        state.voiceProblem?.let {
            Text(voiceProblemMessage(it), color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
        }
        if (state.listening) {
            Text(
                if (state.heard.isEmpty()) stringResource(R.string.tutor_listening) else stringResource(R.string.tutor_heard, state.heard),
                fontStyle = FontStyle.Italic,
            )
            Button(onClick = viewModel::stopVoice, modifier = Modifier.fillMaxWidth()) {
                Text(stringResource(R.string.tutor_stop_voice))
            }
        } else {
            Button(onClick = onAskByVoice, enabled = state.canAsk, modifier = Modifier.fillMaxWidth()) {
                Text(stringResource(R.string.tutor_ask_voice))
            }
        }
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = state.draft,
                onValueChange = viewModel::onDraftChange,
                label = { Text(stringResource(R.string.tutor_question_label)) },
                enabled = !state.listening,
                maxLines = 4,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { viewModel.askDraft() }),
                modifier = Modifier.weight(1f),
            )
            OutlinedButton(onClick = viewModel::askDraft, enabled = state.canAsk && state.draft.isNotBlank()) {
                Text(stringResource(R.string.tutor_ask))
            }
        }
        if (state.speechAvailable) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Switch(checked = state.readAloud, onCheckedChange = viewModel::setReadAloud)
                Text(stringResource(R.string.tutor_read_aloud), style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
                if (state.speaking) {
                    TextButton(onClick = viewModel::stopSpeaking) { Text(stringResource(R.string.tutor_stop_reading)) }
                }
            }
        } else {
            Text(stringResource(R.string.tutor_no_speech_output), style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun voiceProblemMessage(problem: VoiceProblem): String = when (problem) {
    VoiceProblem.NO_SPEECH -> stringResource(R.string.tutor_voice_no_speech)
    VoiceProblem.PERMISSION_DENIED -> stringResource(R.string.tutor_voice_permission)
    VoiceProblem.UNAVAILABLE -> stringResource(R.string.tutor_voice_unavailable)
    VoiceProblem.FAILED -> stringResource(R.string.tutor_voice_failed)
}

/** Stops listening and reading on `ON_STOP` (not on a rotation, which the view model outlives) and when the screen goes away. */
@Composable
private fun QuietInBackground(viewModel: TutorViewModel) {
    val lifecycleOwner = LocalLifecycleOwner.current
    val activity = LocalContext.current.findActivity()
    DisposableEffect(lifecycleOwner, viewModel) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP && activity?.isChangingConfigurations != true) viewModel.onBackground()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            if (activity?.isChangingConfigurations != true) viewModel.onBackground()
        }
    }
}

private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}
