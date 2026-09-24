package com.titanarq.studentassistant.pairing

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.titanarq.studentassistant.R
import java.util.concurrent.Executor
import java.util.concurrent.Executors

/**
 * Feeds each camera frame's luminance plane to [QrDecoder] and reports every decoded text on
 * [callbackExecutor]. Frames arrive on CameraX's analysis thread; each is closed after use.
 */
class QrAnalyzer(
    private val decoder: QrDecoder,
    private val callbackExecutor: Executor,
    private val onDecoded: (String) -> Unit,
) : ImageAnalysis.Analyzer {
    override fun analyze(image: ImageProxy) {
        try {
            val plane = image.planes[0]
            val buffer = plane.buffer
            val bytes = ByteArray(buffer.remaining())
            buffer.get(bytes)
            val text = decoder.decode(bytes, image.width, image.height, plane.rowStride)
            if (text != null) callbackExecutor.execute { onDecoded(text) }
        } finally {
            image.close()
        }
    }
}

/**
 * The pairing QR scanner: asks for the camera permission (with a Spanish rationale), then shows
 * the CameraX preview and decodes QR codes with ZXing. [onQr] runs on the main thread.
 */
@Composable
fun QrScanner(onQr: (String) -> Unit, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED,
        )
    }
    var denied by rememberSaveable { mutableStateOf(false) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        granted = ok
        denied = !ok
    }

    if (granted) {
        CameraPreview(onQr = onQr, modifier = modifier)
    } else {
        Column(modifier = modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(
                text = stringResource(if (denied) R.string.camera_denied else R.string.camera_rationale),
                style = MaterialTheme.typography.bodyMedium,
            )
            Button(onClick = { launcher.launch(Manifest.permission.CAMERA) }) {
                Text(stringResource(R.string.camera_permission_button))
            }
        }
    }
}

@Composable
private fun CameraPreview(onQr: (String) -> Unit, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val currentOnQr by rememberUpdatedState(onQr)
    val previewView = remember { PreviewView(context) }
    var failed by remember { mutableStateOf(false) }

    DisposableEffect(lifecycleOwner) {
        val analysisExecutor = Executors.newSingleThreadExecutor()
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
                    val analysis = ImageAnalysis.Builder()
                        .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                        .build()
                    analysis.setAnalyzer(analysisExecutor, QrAnalyzer(QrDecoder(), mainExecutor) { currentOnQr(it) })
                    cameraProvider.unbindAll()
                    cameraProvider.bindToLifecycle(lifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
                } catch (e: Exception) {
                    // No back camera, camera in use, provider failure: fall back to manual entry.
                    failed = true
                }
            },
            mainExecutor,
        )
        onDispose {
            provider?.unbindAll()
            analysisExecutor.shutdown()
        }
    }

    if (failed) {
        Text(
            text = stringResource(R.string.camera_unavailable),
            style = MaterialTheme.typography.bodyMedium,
            modifier = modifier,
        )
    } else {
        AndroidView(factory = { previewView }, modifier = modifier.fillMaxWidth())
    }
}
