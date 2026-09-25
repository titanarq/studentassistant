package com.titanarq.studentassistant.desk

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.webkit.CookieManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.LinearProgressIndicator
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
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.titanarq.studentassistant.R

/**
 * A topic's notes on the phone (#83): the backend's web notes page, with the editor chat beside
 * it, in a WebView authenticated by the paired token's cookie. Back goes back in the page history,
 * then [onBack].
 */
@Composable
fun StudyDeskScreen(viewModel: StudyDeskViewModel, onBack: () -> Unit, modifier: Modifier = Modifier) {
    val state by viewModel.state.collectAsStateWithLifecycle()

    Surface(modifier = modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.fillMaxSize()) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp),
            ) {
                TextButton(onClick = onBack) { Text(stringResource(R.string.back)) }
                Text(
                    stringResource(R.string.desk_title, viewModel.topic.topicName),
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                if (state is DeskUiState.Ready) {
                    TextButton(onClick = viewModel::retry) { Text(stringResource(R.string.desk_reload)) }
                }
            }
            when (val current = state) {
                DeskUiState.Loading -> Message(stringResource(R.string.loading))
                DeskUiState.NoBackend -> Message(stringResource(R.string.home_no_backend))
                DeskUiState.InvalidBackend -> Message(stringResource(R.string.desk_invalid_backend))
                is DeskUiState.Ready -> DeskWebView(
                    ready = current,
                    onLoadFailed = viewModel::onLoadFailed,
                    onRetry = viewModel::retry,
                    onExit = onBack,
                )
            }
        }
    }
}

@Composable
private fun Message(text: String) {
    Text(text, modifier = Modifier.padding(16.dp))
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
private fun DeskWebView(
    ready: DeskUiState.Ready,
    onLoadFailed: (Int?, String) -> Unit,
    onRetry: () -> Unit,
    onExit: () -> Unit,
) {
    val context = LocalContext.current
    var canGoBack by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(true) }
    val webView = remember {
        WebView(context).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val url = request.url.toString()
                    if (isSameOrigin(url, ready.baseUrl)) return false
                    try {
                        view.context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                    } catch (_: ActivityNotFoundException) {
                        // Nothing can open it: stay on the page.
                    }
                    return true
                }

                override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
                    loading = true
                }

                override fun onPageFinished(view: WebView, url: String?) {
                    loading = false
                }

                override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                    canGoBack = view.canGoBack()
                }

                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                    if (request.isForMainFrame) onLoadFailed(null, error.description?.toString().orEmpty())
                }

                override fun onReceivedHttpError(
                    view: WebView,
                    request: WebResourceRequest,
                    errorResponse: WebResourceResponse,
                ) {
                    if (request.isForMainFrame) onLoadFailed(errorResponse.statusCode, errorResponse.reasonPhrase.orEmpty())
                }
            }
        }
    }

    // The token goes into the WebView's cookie jar only while this screen is shown.
    DisposableEffect(webView) {
        onDispose {
            webView.stopLoading()
            webView.destroy()
            CookieManager.getInstance().apply {
                removeAllCookies(null)
                flush()
            }
        }
    }
    LaunchedEffect(ready.page, ready.reload) {
        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setCookie(ready.page.cookieUrl, ready.page.cookie)
            flush()
        }
        if (ready.reload == 0 || webView.url == null) webView.loadUrl(ready.page.url) else webView.reload()
    }
    BackHandler(enabled = canGoBack) { webView.goBack() }

    Box(modifier = Modifier.fillMaxSize()) {
        AndroidView(factory = { webView }, modifier = Modifier.fillMaxSize())
        if (loading && ready.failure == null) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth().align(Alignment.TopCenter))
        }
        ready.failure?.let { failure ->
            Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text(
                        when (failure) {
                            DeskLoadFailure.Unauthorized -> stringResource(R.string.desk_unauthorized)
                            is DeskLoadFailure.Failed -> stringResource(R.string.desk_load_failed, ready.backendName, failure.detail)
                        },
                        color = MaterialTheme.colorScheme.error,
                    )
                    OutlinedButton(onClick = onRetry) { Text(stringResource(R.string.retry)) }
                    TextButton(onClick = onExit) { Text(stringResource(R.string.back)) }
                }
            }
        }
    }
}
