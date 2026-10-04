package com.titanarq.studentassistant

import android.os.Bundle
import androidx.camera.core.ImageCapture
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.titanarq.studentassistant.backend.ConnectionTestScreen
import com.titanarq.studentassistant.capture.CaptureScreen
import com.titanarq.studentassistant.capture.CaptureViewModel
import com.titanarq.studentassistant.backend.ConnectionTestViewModel
import com.titanarq.studentassistant.backend.PairedBackendsScreen
import com.titanarq.studentassistant.backend.PairedBackendsViewModel
import com.titanarq.studentassistant.home.HomeScreen
import com.titanarq.studentassistant.home.HomeViewModel
import com.titanarq.studentassistant.pairing.PairingScreen
import com.titanarq.studentassistant.pairing.PairingViewModel
import com.titanarq.studentassistant.ui.AppScaffold
import com.titanarq.studentassistant.ui.PlaceholderScreen
import com.titanarq.studentassistant.ui.SettingsAction
import com.titanarq.studentassistant.ui.backRoute
import com.titanarq.studentassistant.ui.Route
import com.titanarq.studentassistant.ui.StudentAssistantTheme
import com.titanarq.studentassistant.ui.routeFor
import com.titanarq.studentassistant.ui.startRoute
import com.titanarq.studentassistant.users.ProfileScreen
import com.titanarq.studentassistant.users.ProfileViewModel
import com.titanarq.studentassistant.users.UserSelectionScreen
import com.titanarq.studentassistant.users.UsersViewModel

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
    // The screen whose gear opened the settings (computers) screen, so its Back returns there.
    var settingsFrom by rememberSaveable { mutableStateOf(Route.HOME) }

    // The selected user lives in memory only: it is empty at every process start.
    val user by container.userHolder.current.collectAsStateWithLifecycle()

    val current = stored
    LaunchedEffect(current == null) {
        if (current != null && route == null) route = startRoute(current, user)
    }
    // The last backend was removed: nothing left to show but pairing.
    LaunchedEffect(current?.backends?.isEmpty()) {
        if (current != null && current.backends.isEmpty()) route = Route.PAIRING
    }
    val hasBackends = current?.backends?.isNotEmpty() == true
    // No user selected (sign-out, switched backend, a refused user): «¿Quién eres?» before any screen that needs one.
    LaunchedEffect(route, hasBackends, user) {
        route?.let { shown -> routeFor(shown, hasBackends, user).let { if (it != shown) route = it } }
    }
    val openSettings = { from: Route ->
        settingsFrom = from
        route = Route.BACKENDS
    }
    // Back (the top bar's icon and the system one) from the screens that have no back of their own.
    val back = route?.let { backRoute(it, hasBackends, settingsFrom) }
    BackHandler(enabled = back != null) { back?.let { route = it } }
    val onBack: (() -> Unit)? = back?.let { target -> { route = target } }

    when (route) {
        null -> PlaceholderScreen()
        Route.PAIRING -> AppScaffold(stringResource(R.string.pairing_title), onBack) { padding ->
            PairingScreen(
                viewModel = viewModel<PairingViewModel>(factory = container.pairingViewModelFactory),
                onDone = { route = Route.CONNECTION_TEST },
                modifier = Modifier.padding(padding),
            )
        }
        Route.HOME -> HomeScreen(
            viewModel = viewModel<HomeViewModel>(factory = container.homeViewModelFactory),
            onSessionOpened = { route = Route.CAPTURE },
            onBackends = { openSettings(Route.HOME) },
            photos = container.userPhotos,
            onEditProfile = { route = Route.PROFILE },
        )
        Route.USERS -> AppScaffold(
            title = stringResource(R.string.users_title),
            onBack = null,
            actions = { SettingsAction { openSettings(Route.USERS) } },
        ) { padding ->
            UserSelectionScreen(
                viewModel = viewModel<UsersViewModel>(factory = container.usersViewModelFactory),
                photos = container.userPhotos,
                onSelected = { route = Route.HOME },
                modifier = Modifier.padding(padding),
            )
        }
        Route.PROFILE -> AppScaffold(stringResource(R.string.users_edit_profile), onBack) { padding ->
            ProfileScreen(
                viewModel = viewModel<ProfileViewModel>(factory = container.profileViewModelFactory),
                photos = container.userPhotos,
                onDone = { route = Route.HOME },
                modifier = Modifier.padding(padding),
            )
        }
        Route.CAPTURE -> {
            val open by container.sessionHolder.current.collectAsStateWithLifecycle()
            val session = open
            if (session == null) {
                LaunchedEffect(Unit) { route = Route.HOME }
            } else {
                // One view model per session; leaving the screen ends the session.
                CaptureScreen(
                    viewModel = viewModel<CaptureViewModel>(
                        key = "capture-${session.session.sessionId}",
                        factory = container.captureViewModelFactory(session),
                    ),
                    user = user,
                    photos = container.userPhotos,
                    onLeave = { route = Route.HOME },
                    onSettings = { openSettings(Route.HOME) },
                    onProfile = { route = Route.PROFILE },
                    imageCapture = imageCapture,
                )
            }
        }
        Route.BACKENDS -> AppScaffold(stringResource(R.string.backends_title), onBack) { padding ->
            PairedBackendsScreen(
                viewModel = backendsViewModel,
                onPairNew = { route = Route.PAIRING },
                onTestConnection = { route = Route.CONNECTION_TEST },
                modifier = Modifier.padding(padding),
            )
        }
        Route.CONNECTION_TEST -> AppScaffold(stringResource(R.string.connection_title), onBack) { padding ->
            ConnectionTestScreen(
                viewModel = viewModel<ConnectionTestViewModel>(factory = container.connectionTestViewModelFactory),
                modifier = Modifier.padding(padding),
            )
        }
    }
}
