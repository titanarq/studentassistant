package com.titanarq.studentassistant

import android.os.Bundle
import androidx.camera.core.ImageCapture
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.titanarq.studentassistant.backend.ConnectionTestScreen
import com.titanarq.studentassistant.capture.CaptureScreen
import com.titanarq.studentassistant.capture.CaptureViewModel
import com.titanarq.studentassistant.backend.ConnectionTestViewModel
import com.titanarq.studentassistant.backend.PairedBackendsScreen
import com.titanarq.studentassistant.backend.PairedBackendsViewModel
import com.titanarq.studentassistant.desk.DeskTopic
import com.titanarq.studentassistant.desk.StudyDeskScreen
import com.titanarq.studentassistant.desk.StudyDeskViewModel
import com.titanarq.studentassistant.home.HomeScreen
import com.titanarq.studentassistant.home.HomeViewModel
import com.titanarq.studentassistant.pairing.PairingScreen
import com.titanarq.studentassistant.pairing.PairingViewModel
import com.titanarq.studentassistant.ui.PlaceholderScreen
import com.titanarq.studentassistant.ui.Route
import com.titanarq.studentassistant.ui.StudentAssistantTheme
import com.titanarq.studentassistant.ui.startRoute

class MainActivity : ComponentActivity() {
    /** The single app-wide container; screens get their dependencies from it. */
    private val container: AppContainer by lazy { (application as StudentAssistantApp).container }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        container // resolve it now so a misregistered Application fails at startup
        setContent {
            StudentAssistantTheme {
                App(container, (application as StudentAssistantApp).stillCamera.imageCapture)
            }
        }
    }
}

/** Opens pairing when no backend is stored, else the subjects/topics home; routes between the screens. */
@Composable
private fun App(container: AppContainer, imageCapture: ImageCapture) {
    val backendsViewModel: PairedBackendsViewModel = viewModel(factory = container.pairedBackendsViewModelFactory)
    val stored by backendsViewModel.backends.collectAsStateWithLifecycle()
    var route by rememberSaveable { mutableStateOf<Route?>(null) }
    // The topic Route.DESK shows, as [subjectId, topicId, topicName] so it survives recreation.
    var deskTopic by rememberSaveable { mutableStateOf<List<String>?>(null) }

    val current = stored
    LaunchedEffect(current == null) {
        if (current != null && route == null) route = startRoute(current)
    }
    // The last backend was removed: nothing left to show but pairing.
    LaunchedEffect(current?.backends?.isEmpty()) {
        if (current != null && current.backends.isEmpty()) route = Route.PAIRING
    }
    val hasBackends = current?.backends?.isNotEmpty() == true
    // Back from the connection test returns to the backends; from anywhere else, to the home.
    BackHandler(enabled = route != null && route != Route.HOME && hasBackends) {
        route = if (route == Route.CONNECTION_TEST) Route.BACKENDS else Route.HOME
    }

    when (route) {
        null -> PlaceholderScreen()
        Route.PAIRING -> PairingScreen(
            viewModel = viewModel<PairingViewModel>(factory = container.pairingViewModelFactory),
            onDone = { route = Route.CONNECTION_TEST },
            onBack = if (hasBackends) ({ route = Route.HOME }) else null,
        )
        Route.HOME -> HomeScreen(
            viewModel = viewModel<HomeViewModel>(factory = container.homeViewModelFactory),
            onSessionOpened = { route = Route.CAPTURE },
            onBackends = { route = Route.BACKENDS },
            onReadNotes = { topic ->
                deskTopic = listOf(topic.subjectId, topic.topicId, topic.topicName)
                route = Route.DESK
            },
        )
        Route.DESK -> {
            val topic = deskTopic?.takeIf { it.size == 3 }?.let { DeskTopic(it[0], it[1], it[2]) }
            if (topic == null) {
                LaunchedEffect(Unit) { route = Route.HOME }
            } else {
                StudyDeskScreen(
                    viewModel = viewModel<StudyDeskViewModel>(
                        key = "desk-${topic.subjectId}/${topic.topicId}",
                        factory = container.studyDeskViewModelFactory(topic),
                    ),
                    onBack = { route = Route.HOME },
                )
            }
        }
        Route.CAPTURE -> {
            val open by container.sessionHolder.current.collectAsStateWithLifecycle()
            val session = open
            if (session == null) {
                LaunchedEffect(Unit) { route = Route.HOME }
            } else {
                // One view model per session: "Continuar" on the same session keeps its transcript.
                CaptureScreen(
                    viewModel = viewModel<CaptureViewModel>(
                        key = "capture-${session.session.sessionId}",
                        factory = container.captureViewModelFactory(session),
                    ),
                    onLeave = { route = Route.HOME },
                    onEnded = { route = Route.HOME },
                    imageCapture = imageCapture,
                )
            }
        }
        Route.BACKENDS -> PairedBackendsScreen(
            viewModel = backendsViewModel,
            onPairNew = { route = Route.PAIRING },
            onTestConnection = { route = Route.CONNECTION_TEST },
        )
        Route.CONNECTION_TEST -> ConnectionTestScreen(
            viewModel = viewModel<ConnectionTestViewModel>(factory = container.connectionTestViewModelFactory),
            onBack = { route = Route.BACKENDS },
        )
    }
}
