package com.titanarq.studentassistant.share

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.titanarq.studentassistant.users.UserSelectionScreen
import com.titanarq.studentassistant.users.UsersViewModel
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
                // The same «¿Quién eres?» as the app, before the subjects: the shared page is kept for a user.
                val user by container.userHolder.current.collectAsStateWithLifecycle()
                if (user == null) {
                    UserSelectionScreen(
                        viewModel = viewModel<UsersViewModel>(factory = container.usersViewModelFactory),
                        photos = container.userPhotos,
                        onSelected = {},
                    )
                } else {
                    ShareScreen(
                        viewModel = viewModel<ShareViewModel>(factory = container.shareViewModelFactory(text, subject)),
                        onClose = ::finish,
                    )
                }
            }
        }
    }
}
