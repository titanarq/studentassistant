package com.titanarq.studentassistant.capture

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R
import com.titanarq.studentassistant.protocol.User
import com.titanarq.studentassistant.ui.AppScaffold
import com.titanarq.studentassistant.ui.SessionActions
import com.titanarq.studentassistant.users.UserPhotos

/**
 * The capture screen (#575), nothing but the capture: the camera preview, the «Capturar» button
 * BELOW it (outside the preview, so nothing covers it), and at the very bottom a strip with the
 * thumbnails of the photos taken. Entering the screen starts the capture session and leaving it
 * ([onLeave], through the top bar's Back or the system back) ends it ([CaptureViewModel.endOnLeave]).
 * The transcription and the pending-doubts counter keep running in the view model, but nothing on
 * the screen shows them. Keeps the screen on. Asks for the camera and microphone at runtime; the
 * session runs even when the microphone is refused (it then has no transcript).
 *
 * [imageCapture] (the still camera's use case) is bound next to the preview; each thumbnail shows
 * its upload state (a tap on a failed one retries it).
 */
@Composable
fun CaptureScreen(
    viewModel: CaptureViewModel,
    user: User?,
    photos: UserPhotos,
    onLeave: () -> Unit,
    onSettings: () -> Unit,
    onProfile: () -> Unit,
    modifier: Modifier = Modifier,
    imageCapture: ImageCapture? = null,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val shots by viewModel.shots.collectAsStateWithLifecycle()
    val context = LocalContext.current
    fun granted(permission: String) =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED
    var cameraGranted by remember { mutableStateOf(granted(Manifest.permission.CAMERA)) }
    var asked by rememberSaveable { mutableStateOf(false) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        asked = true
        cameraGranted = result[Manifest.permission.CAMERA] ?: cameraGranted
    }
    val askPermissions = {
        launcher.launch(arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO))
    }

    KeepScreenOn()
    PauseInBackground(viewModel)
    // Entering the screen starts the capture session.
    LaunchedEffect(Unit) {
        viewModel.start()
        if (!asked && (!cameraGranted || !granted(Manifest.permission.RECORD_AUDIO))) askPermissions()
    }
    // Leaving it ends the session.
    val leave = {
        viewModel.endOnLeave()
        onLeave()
    }
    BackHandler(onBack = leave)

    AppScaffold(
        title = stringResource(R.string.capture_title, state.subjectName, state.topicName),
        onBack = leave,
        modifier = modifier,
        actions = {
            // Both open another screen, which also leaves the capture: the session ends.
            SessionActions(
                user,
                photos,
                onSettings = { viewModel.endOnLeave(); onSettings() },
                onProfile = { viewModel.endOnLeave(); onProfile() },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier.padding(padding).fillMaxSize().padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Box(
                modifier = Modifier.fillMaxWidth().weight(1f).clip(MaterialTheme.shapes.medium),
            ) {
                if (cameraGranted) {
                    SessionCameraPreview(imageCapture, modifier = Modifier.fillMaxSize())
                } else {
                    Column(
                        modifier = Modifier.align(Alignment.Center).padding(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        Text(stringResource(if (asked) R.string.capture_camera_denied else R.string.capture_camera_rationale))
                        Button(onClick = askPermissions) { Text(stringResource(R.string.capture_permission_button)) }
                    }
                }
            }
            Button(
                onClick = viewModel::capture,
                enabled = captureEnabled(state.phase),
                modifier = Modifier.fillMaxWidth().height(64.dp),
            ) {
                Text(stringResource(R.string.capture_button_capture), style = MaterialTheme.typography.titleMedium)
            }
            ThumbnailStrip(shots, onRetry = viewModel::retryShot, modifier = Modifier.fillMaxWidth().height(STRIP_HEIGHT))
        }
    }
}

/** «Capturar» works while the session runs (it starts as the screen is entered, whatever the permissions). */
internal fun captureEnabled(phase: CapturePhase): Boolean = phase == CapturePhase.RUNNING

private val STRIP_HEIGHT = 80.dp
private val THUMBNAIL_SIZE = 72.dp

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

/**
 * The photos taken, newest last, each with its upload state. Every item is its own keyed composable
 * ([Thumbnail]), so a gesture on one (a later task: drag it down to discard it) can be added to
 * it without touching the strip.
 */
@Composable
private fun ThumbnailStrip(shots: List<CaptureShot>, onRetry: (String) -> Unit, modifier: Modifier = Modifier) {
    val listState = rememberLazyListState()
    LaunchedEffect(shots.size) { if (shots.isNotEmpty()) listState.animateScrollToItem(shots.lastIndex) }
    LazyRow(
        state = listState,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically,
        modifier = modifier,
    ) {
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
            .size(THUMBNAIL_SIZE)
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
