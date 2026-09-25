package com.titanarq.studentassistant.share

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.viewmodel.compose.viewModel
import com.titanarq.studentassistant.AppContainer
import com.titanarq.studentassistant.StudentAssistantApp
import com.titanarq.studentassistant.ui.StudentAssistantTheme

/**
 * The target of "Compartir -> Student Assistant" (`ACTION_SEND`, `text/plain`): a browser or any
 * app shares a link and the student saves the page to one of their topics ([ShareScreen]).
 */
class ShareActivity : ComponentActivity() {
    private val container: AppContainer by lazy { (application as StudentAssistantApp).container }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val text = intent?.takeIf { it.action == Intent.ACTION_SEND }?.getStringExtra(Intent.EXTRA_TEXT)
        val subject = intent?.takeIf { it.action == Intent.ACTION_SEND }?.getStringExtra(Intent.EXTRA_SUBJECT)
        setContent {
            StudentAssistantTheme {
                ShareScreen(
                    viewModel = viewModel<ShareViewModel>(factory = container.shareViewModelFactory(text, subject)),
                    onClose = ::finish,
                )
            }
        }
    }
}
