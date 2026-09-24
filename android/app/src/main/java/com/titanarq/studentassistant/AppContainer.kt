package com.titanarq.studentassistant

import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.ConnectionTestViewModel
import com.titanarq.studentassistant.backend.OkHttpBackendClient
import com.titanarq.studentassistant.backend.PairedBackendsViewModel
import com.titanarq.studentassistant.home.HomeViewModel
import com.titanarq.studentassistant.pairing.PairingViewModel
import com.titanarq.studentassistant.session.SessionHolder
import java.io.File

/** Wall-clock time source, so time-dependent logic can be tested with a fake. */
fun interface Clock {
    fun nowMillis(): Long
}

/** The real [Clock], backed by [System.currentTimeMillis]. */
object SystemClock : Clock {
    override fun nowMillis(): Long = System.currentTimeMillis()
}

/**
 * Manual constructor DI (AGENTS.md): the app-wide object graph, created once by
 * [StudentAssistantApp]. Plain Kotlin with no Android framework types so it is testable on the
 * JVM; members are `by lazy`, and tests pass constructor overrides for fakes.
 *
 * @param filesDir the app's private files directory (`Context.filesDir`), home of the
 *   paired-backends DataStore file.
 * @param deviceName the name this phone pairs under (`Build.MODEL` on a device).
 */
class AppContainer(
    private val filesDir: File,
    val deviceName: String = "Android",
    clockFactory: () -> Clock = { SystemClock },
    backendClientFactory: () -> BackendClient = { OkHttpBackendClient() },
    backendStoreFactory: (File) -> BackendStore = { dir -> BackendStore.create(File(dir, BackendStore.FILE_NAME)) },
) {
    /** The app-wide clock, created on first access and shared afterwards. */
    val clock: Clock by lazy(clockFactory)

    /** The one HTTP client of every REST call to the backends. */
    val backendClient: BackendClient by lazy(backendClientFactory)

    /** The paired backends (one DataStore per file, so exactly one store per process). */
    val backendStore: BackendStore by lazy { backendStoreFactory(filesDir) }

    /** Creates the pairing screen's [PairingViewModel]. */
    val pairingViewModelFactory: ViewModelProvider.Factory by lazy {
        viewModelFactory { initializer { PairingViewModel(backendClient, backendStore, deviceName) } }
    }

    /** Creates the paired-backends screen's [PairedBackendsViewModel]. */
    val pairedBackendsViewModelFactory: ViewModelProvider.Factory by lazy {
        viewModelFactory { initializer { PairedBackendsViewModel(backendStore) } }
    }

    /** Creates the connection-test screen's [ConnectionTestViewModel]. */
    val connectionTestViewModelFactory: ViewModelProvider.Factory by lazy {
        viewModelFactory { initializer { ConnectionTestViewModel(backendClient, backendStore) } }
    }

    /** The session the capture screen works on, handed over by the home screen. */
    val sessionHolder: SessionHolder by lazy { SessionHolder() }

    /** Creates the home screen's [HomeViewModel]. */
    val homeViewModelFactory: ViewModelProvider.Factory by lazy {
        viewModelFactory { initializer { HomeViewModel(backendClient, backendStore, sessionHolder, clock) } }
    }
}
