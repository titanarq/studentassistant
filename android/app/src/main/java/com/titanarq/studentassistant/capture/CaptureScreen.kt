package com.titanarq.studentassistant.capture

import android.Manifest
import android.content.pm.PackageManager
import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import android.graphics.BitmapFactory
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.protocol.SourceKind
import com.titanarq.studentassistant.ui.backendFailureMessage

/**
 * The session screen: camera preview, live transcript, pending-doubts counter and the session
 * buttons. Asks for the camera and microphone at runtime; the session starts once the microphone
 * is granted. Keeps the screen on. [onLeave] (back) leaves the session open; [onEnded] follows
 * "Terminar". [imageCapture] (the still camera's use case) is bound next to the preview; the
 * thumbnail strip shows each capture's upload state (a tap on a failed one retries it).
 */
@Composable
fun CaptureScreen(
    viewModel: CaptureViewModel,
    onLeave: () -> Unit,
    onEnded: () -> Unit,
    modifier: Modifier = Modifier,
    imageCapture: ImageCapture? = null,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val shots by viewModel.shots.collectAsStateWithLifecycle()
    val context = LocalContext.current
    fun granted(permission: String) =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED
    var cameraGranted by remember { mutableStateOf(granted(Manifest.permission.CAMERA)) }
    var micGranted by remember { mutableStateOf(granted(Manifest.permission.RECORD_AUDIO)) }
    var asked by rememberSaveable { mutableStateOf(false) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        asked = true
        cameraGranted = result[Manifest.permission.CAMERA] ?: cameraGranted
        micGranted = result[Manifest.permission.RECORD_AUDIO] ?: micGranted
    }
    val askPermissions = {
        launcher.launch(arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO))
    }

    KeepScreenOn()
    PauseInBackground(viewModel)
    LaunchedEffect(micGranted) { if (micGranted) viewModel.start() }
    LaunchedEffect(state.phase) { if (state.phase == CapturePhase.ENDED) onEnded() }
    val leave = {
        viewModel.leave()
        onLeave()
    }
    BackHandler(onBack = leave)
    var confirmEnd by rememberSaveable { mutableStateOf(false) }

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Header(state, onLeave = leave, onRetry = viewModel::retry)
            if (!cameraGranted || !micGranted) {
                PermissionRationale(cameraGranted, micGranted, denied = asked, onAsk = askPermissions)
            }
            Box(modifier = Modifier.fillMaxWidth().weight(1f)) {
                if (cameraGranted) {
                    SessionCameraPreview(imageCapture, modifier = Modifier.fillMaxSize())
                } else {
                    Text(stringResource(R.string.capture_no_camera), modifier = Modifier.align(Alignment.Center))
                }
            }
            if (state.spoolNearCap) {
                Text(stringResource(R.string.capture_spool_near_cap), color = MaterialTheme.colorScheme.error)
            }
            if (state.micPaused) {
                Text(stringResource(R.string.capture_mic_paused), color = MaterialTheme.colorScheme.primary)
            }
            state.micProblem?.let {
                Text(
                    stringResource(
                        if (it == MicProblem.PERMISSION_DENIED) R.string.capture_mic_denied else R.string.capture_mic_unavailable,
                    ),
                    color = MaterialTheme.colorScheme.error,
                )
            }
            state.endFailure?.let {
                Text(
                    stringResource(R.string.capture_end_failed, backendFailureMessage(it)),
                    color = MaterialTheme.colorScheme.error,
                )
            }
            if (shots.isNotEmpty()) ThumbnailStrip(shots, onRetry = viewModel::retryShot)
            Transcript(state.transcript, modifier = Modifier.fillMaxWidth().weight(1f))
            SessionButtons(
                state = state,
                onCapture = viewModel::capture,
                onImportant = viewModel::important,
                onToggleSource = viewModel::toggleSource,
                onEnd = { confirmEnd = true },
            )
        }
    }

    if (confirmEnd) {
        AlertDialog(
            onDismissRequest = { confirmEnd = false },
            title = { Text(stringResource(R.string.capture_end_title)) },
            text = { Text(stringResource(R.string.capture_end_text)) },
            confirmButton = {
                TextButton(onClick = {
                    confirmEnd = false
                    viewModel.end()
                }) { Text(stringResource(R.string.capture_end_confirm)) }
            },
            dismissButton = {
                TextButton(onClick = { confirmEnd = false }) { Text(stringResource(R.string.cancel)) }
            },
        )
    }
}

@Composable
private fun KeepScreenOn() {
    val view = LocalView.current
    DisposableEffect(view) {
        view.keepScreenOn = true
        onDispose { view.keepScreenOn = false }
    }
}

/**
 * Stops the microphone on `ON_STOP` and restarts it on `ON_START` (the camera preview follows the
 * same lifecycle through CameraX). A rotation is not a trip to the background: the view model
 * outlives it and the microphone keeps running.
 */
@Composable
private fun PauseInBackground(viewModel: CaptureViewModel) {
    val lifecycleOwner = LocalLifecycleOwner.current
    val activity = LocalContext.current.findActivity()
    DisposableEffect(lifecycleOwner, viewModel) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_STOP -> if (activity?.isChangingConfigurations != true) viewModel.onBackground()
                Lifecycle.Event.ON_START -> viewModel.onForeground()
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
}

private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}

@Composable
private fun Header(state: CaptureUiState, onLeave: () -> Unit, onRetry: () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                stringResource(R.string.capture_title, state.subjectName, state.topicName),
                style = MaterialTheme.typography.titleMedium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(connectionText(state.connection), style = MaterialTheme.typography.bodySmall)
        }
        state.pendingCount?.let {
            Text(
                pluralStringResource(R.plurals.capture_pending, it, it),
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.padding(horizontal = 8.dp),
            )
        }
        if (state.connection is ConnectionState.Failed) {
            TextButton(onClick = onRetry) { Text(stringResource(R.string.retry)) }
        }
        TextButton(onClick = onLeave) { Text(stringResource(R.string.capture_leave)) }
    }
}

@Composable
private fun connectionText(connection: ConnectionState): String = when (connection) {
    ConnectionState.Connecting -> stringResource(R.string.capture_connecting)
    is ConnectionState.Connected -> stringResource(R.string.capture_connected)
    is ConnectionState.Reconnecting -> stringResource(R.string.capture_reconnecting, connection.attempt)
    is ConnectionState.Failed -> when (connection.failure) {
        is ConnectionFailure.Unauthorized -> stringResource(R.string.capture_failed_unauthorized)
        ConnectionFailure.SessionNotActive -> stringResource(R.string.capture_failed_not_active)
        is ConnectionFailure.Refused -> stringResource(R.string.capture_failed_refused, connection.failure.reason)
    }
    ConnectionState.Stopped -> stringResource(R.string.capture_stopped)
}

@Composable
private fun PermissionRationale(camera: Boolean, mic: Boolean, denied: Boolean, onAsk: () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        if (!mic) Text(stringResource(if (denied) R.string.capture_mic_permission_denied else R.string.capture_mic_rationale))
        if (!camera) Text(stringResource(if (denied) R.string.capture_camera_denied else R.string.capture_camera_rationale))
        Button(onClick = onAsk) { Text(stringResource(R.string.capture_permission_button)) }
    }
}

@Composable
private fun Transcript(lines: List<TranscriptLine>, modifier: Modifier = Modifier) {
    val listState = rememberLazyListState()
    LaunchedEffect(lines.size, lines.lastOrNull()?.text) {
        if (lines.isNotEmpty()) listState.animateScrollToItem(lines.lastIndex)
    }
    Surface(modifier = modifier, color = Color.White, shape = MaterialTheme.shapes.medium) {
        if (lines.isEmpty()) {
            Box(modifier = Modifier.fillMaxSize().padding(12.dp)) {
                Text(stringResource(R.string.capture_transcript_empty), color = Color.Gray)
            }
        } else {
            LazyColumn(state = listState, modifier = Modifier.padding(12.dp)) {
                items(lines, key = { it.segmentId }) { line ->
                    Text(line.text, color = if (line.final) Color.Black else Color.Gray)
                }
            }
        }
    }
}

@Composable
private fun SessionButtons(
    state: CaptureUiState,
    onCapture: () -> Unit,
    onImportant: () -> Unit,
    onToggleSource: () -> Unit,
    onEnd: () -> Unit,
) {
    val enabled = state.phase == CapturePhase.RUNNING
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Button(onClick = onCapture, enabled = enabled, modifier = Modifier.weight(1f)) {
                Text(stringResource(R.string.capture_button_capture))
            }
            FilledTonalButton(onClick = onImportant, enabled = enabled, modifier = Modifier.weight(1f)) {
                Text(stringResource(R.string.capture_button_important))
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            OutlinedButton(onClick = onToggleSource, enabled = enabled, modifier = Modifier.weight(1f)) {
                Text(
                    stringResource(
                        if (state.source == SourceKind.BOOK) R.string.capture_source_book else R.string.capture_source_notes,
                    ),
                )
            }
            OutlinedButton(onClick = onEnd, enabled = enabled, modifier = Modifier.weight(1f)) {
                Text(
                    stringResource(
                        if (state.phase == CapturePhase.ENDING) R.string.capture_ending else R.string.capture_button_end,
                    ),
                )
            }
        }
    }
}

/** The back camera's CameraX preview and [imageCapture], bound to the screen's lifecycle. */
@Composable
private fun SessionCameraPreview(imageCapture: ImageCapture?, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val previewView = remember { PreviewView(context) }
    var failed by remember { mutableStateOf(false) }

    DisposableEffect(lifecycleOwner, imageCapture) {
        val mainExecutor = ContextCompat.getMainExecutor(context)
        val future = ProcessCameraProvider.getInstance(context)
        var provider: ProcessCameraProvider? = null
        future.addListener(
            {
                try {
                    val cameraProvider = future.get()
                    provider = cameraProvider
                    val preview = Preview.Builder().build()
                    preview.setSurfaceProvider(previewView.surfaceProvider)
                    cameraProvider.unbindAll()
                    val useCases = listOfNotNull(preview, imageCapture)
                    previewView.display?.let { display -> imageCapture?.targetRotation = display.rotation }
                    cameraProvider.bindToLifecycle(lifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, *useCases.toTypedArray())
                } catch (e: Exception) {
                    failed = true
                }
            },
            mainExecutor,
        )
        onDispose { provider?.unbindAll() }
    }

    if (failed) {
        Box(modifier = modifier.background(Color.DarkGray)) {
            Text(
                stringResource(R.string.camera_unavailable_capture),
                color = Color.White,
                modifier = Modifier.align(Alignment.Center).padding(12.dp),
            )
        }
    } else {
        AndroidView(factory = { previewView }, modifier = modifier)
    }
}

/** The session's captures, newest last, each with its upload state. */
@Composable
private fun ThumbnailStrip(shots: List<CaptureShot>, onRetry: (String) -> Unit) {
    val listState = rememberLazyListState()
    LaunchedEffect(shots.size) { listState.animateScrollToItem(shots.lastIndex) }
    LazyRow(state = listState, horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
        items(shots, key = { it.captureId }) { shot -> Thumbnail(shot, onRetry) }
    }
}

@Composable
private fun Thumbnail(shot: CaptureShot, onRetry: (String) -> Unit) {
    val bitmap = remember(shot.captureId, shot.thumbnail) {
        shot.thumbnail?.bytes?.let { BitmapFactory.decodeByteArray(it, 0, it.size)?.asImageBitmap() }
    }
    val description = stringResource(
        when (shot.status) {
            ShotStatus.CAPTURING -> R.string.capture_shot_capturing
            ShotStatus.PENDING -> R.string.capture_shot_pending
            ShotStatus.UPLOADING -> R.string.capture_shot_uploading
            ShotStatus.UPLOADED -> R.string.capture_shot_uploaded
            ShotStatus.FAILED -> R.string.capture_shot_failed
            ShotStatus.CAMERA_FAILED -> R.string.capture_shot_camera_failed
        },
    )
    val clickable = if (shot.status == ShotStatus.FAILED) Modifier.clickable { onRetry(shot.captureId) } else Modifier
    Box(
        modifier = Modifier
            .size(64.dp)
            .clip(MaterialTheme.shapes.small)
            .background(Color.DarkGray)
            .then(clickable)
            .semantics { contentDescription = description },
    ) {
        if (bitmap != null) {
            Image(bitmap, contentDescription = null, contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize())
        }
        val badge = Modifier.align(Alignment.BottomEnd).padding(2.dp)
        when (shot.status) {
            ShotStatus.CAPTURING, ShotStatus.UPLOADING ->
                CircularProgressIndicator(modifier = badge.size(16.dp), strokeWidth = 2.dp, color = Color.White)
            ShotStatus.PENDING -> StatusBadge("↑", Color(0xFF8D6E00), badge)
            ShotStatus.UPLOADED -> StatusBadge("✓", Color(0xFF2E7D32), badge)
            ShotStatus.FAILED, ShotStatus.CAMERA_FAILED -> StatusBadge("!", MaterialTheme.colorScheme.error, badge)
        }
    }
}

@Composable
private fun StatusBadge(symbol: String, color: Color, modifier: Modifier) {
    Surface(color = color, shape = MaterialTheme.shapes.extraSmall, modifier = modifier) {
        Text(symbol, color = Color.White, style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(horizontal = 4.dp))
    }
}
