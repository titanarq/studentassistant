package com.titanarq.studentassistant

import android.os.Bundle
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
import com.titanarq.studentassistant.backend.ConnectionTestViewModel
import com.titanarq.studentassistant.backend.PairedBackendsScreen
import com.titanarq.studentassistant.backend.PairedBackendsViewModel
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
                App(container)
            }
        }
    }
}

/** Opens pairing when no backend is stored, else the paired backends; routes between them. */
@Composable
private fun App(container: AppContainer) {
    val backendsViewModel: PairedBackendsViewModel = viewModel(factory = container.pairedBackendsViewModelFactory)
    val stored by backendsViewModel.backends.collectAsStateWithLifecycle()
    var route by rememberSaveable { mutableStateOf<Route?>(null) }

    val current = stored
    LaunchedEffect(current == null) {
        if (current != null && route == null) route = startRoute(current)
    }
    // The last backend was removed: nothing left to show but pairing.
    LaunchedEffect(current?.backends?.isEmpty()) {
        if (current != null && current.backends.isEmpty()) route = Route.PAIRING
    }
    val hasBackends = current?.backends?.isNotEmpty() == true
    BackHandler(enabled = route != null && route != Route.BACKENDS && hasBackends) {
        route = Route.BACKENDS
    }

    when (route) {
        null -> PlaceholderScreen()
        Route.PAIRING -> PairingScreen(
            viewModel = viewModel<PairingViewModel>(factory = container.pairingViewModelFactory),
            onDone = { route = Route.CONNECTION_TEST },
            onBack = if (hasBackends) ({ route = Route.BACKENDS }) else null,
        )
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
