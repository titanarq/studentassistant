package com.titanarq.studentassistant

import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.ConnectionTestViewModel
import com.titanarq.studentassistant.backend.OkHttpBackendClient
import com.titanarq.studentassistant.backend.PairedBackendsViewModel
import com.titanarq.studentassistant.capture.AudioSource
import com.titanarq.studentassistant.capture.AudioStreamer
import com.titanarq.studentassistant.capture.BurstStillCapture
import com.titanarq.studentassistant.capture.CaptureFeedback
import com.titanarq.studentassistant.capture.CaptureSpooling
import com.titanarq.studentassistant.capture.CaptureUploadQueue
import com.titanarq.studentassistant.capture.CaptureViewModel
import com.titanarq.studentassistant.capture.ClientTranscriber
import com.titanarq.studentassistant.capture.NoCaptureFeedback
import com.titanarq.studentassistant.capture.NoStillCamera
import com.titanarq.studentassistant.capture.OkHttpSessionSocketFactory
import com.titanarq.studentassistant.capture.SessionFinisher
import com.titanarq.studentassistant.capture.SessionSocketFactory
import com.titanarq.studentassistant.capture.StillCamera
import com.titanarq.studentassistant.home.HomeViewModel
import com.titanarq.studentassistant.pairing.PairingViewModel
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.session.SessionHolder
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.spool.SpoolBudget
import com.titanarq.studentassistant.spool.Spools
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlin.coroutines.CoroutineContext

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
 * @param clientTranscriberFactory the capture screen's speech recognizer (Android's
 *   `SpeechRecognizer` on a device, built by [StudentAssistantApp] with a `Context`), given the
 *   view model's scope.
 * @param audioSourceFactory the microphone for server STT mode (`AudioRecord` on a device).
 * @param stillCameraFactory the camera of still capture (CameraX `ImageCapture` on a device).
 * @param captureFeedbackFactory vibration + shutter sound on a capture trigger.
 * @param uploadScope the app-wide scope capture uploads run on, so they outlive the capture screen.
 * @param spoolMaxBytes the byte cap of the offline spool (audio of every session plus pending
 *   captures) under `filesDir/spool`.
 * @param ioContext where disk-backed session connections run (never the main thread).
 */
class AppContainer(
    private val filesDir: File,
    val deviceName: String = "Android",
    clockFactory: () -> Clock = { SystemClock },
    backendClientFactory: () -> BackendClient = { OkHttpBackendClient() },
    backendStoreFactory: (File) -> BackendStore = { dir -> BackendStore.create(File(dir, BackendStore.FILE_NAME)) },
    sessionSocketFactoryFactory: () -> SessionSocketFactory = { OkHttpSessionSocketFactory() },
    private val clientTranscriberFactory: (CoroutineScope) -> ClientTranscriber =
        { error("no speech recognizer configured") },
    private val audioSourceFactory: () -> AudioSource = { error("no microphone configured") },
    stillCameraFactory: () -> StillCamera = { NoStillCamera },
    captureFeedbackFactory: () -> CaptureFeedback = { NoCaptureFeedback },
    private val uploadScope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
    private val spoolMaxBytes: Long = SpoolBudget.DEFAULT_MAX_BYTES,
    private val ioContext: CoroutineContext = Dispatchers.IO,
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

    /** The camera every capture burst is taken with. */
    val stillCamera: StillCamera by lazy(stillCameraFactory)

    /** The capture trigger's vibration and shutter sound. */
    val captureFeedback: CaptureFeedback by lazy(captureFeedbackFactory)

    /** The offline spool (android-offline, #53): `filesDir/spool`, capped at [spoolMaxBytes]. */
    val spools: Spools by lazy { Spools(File(filesDir, Spools.DIR_NAME), SpoolBudget(spoolMaxBytes)) }

    /** Uploads every capture burst, with retries, on [uploadScope]; pending ones are spooled to disk. */
    val captureUploads: CaptureUploadQueue by lazy {
        CaptureUploadQueue(uploadScope, backendClient, spool = spools.captures)
    }

    /** Opens the session WebSockets of the capture screen. */
    val sessionSocketFactory: SessionSocketFactory by lazy(sessionSocketFactoryFactory)

    /** Completes the sessions ended while the backend could not be told. */
    val sessionFinisher: SessionFinisher by lazy {
        SessionFinisher(
            scope = uploadScope,
            client = backendClient,
            spools = spools,
            uploads = captureUploads,
            socketFactory = sessionSocketFactory,
            clock = clock,
            credentials = ::credentialsFor,
            loopContext = ioContext,
        )
    }

    /**
     * At app start: queues again the captures an earlier run left unsent and resumes its pending
     * session ends, on [uploadScope].
     */
    fun recoverSpool() {
        uploadScope.launch(ioContext) {
            captureUploads.restore(::credentialsFor)
            sessionFinisher.restore()
        }
    }

    private suspend fun credentialsFor(baseUrl: String): BackendCredentials? =
        backendStore.current().backends.firstOrNull { it.baseUrl == baseUrl }?.credentials

    /** Creates the capture screen's [CaptureViewModel] for [session]. */
    fun captureViewModelFactory(session: OpenSession): ViewModelProvider.Factory = viewModelFactory {
        initializer {
            CaptureViewModel(
                open = session,
                backendClient = backendClient,
                sessionHolder = sessionHolder,
                clock = clock,
                socketFactory = sessionSocketFactory,
                transcriberFactory = clientTranscriberFactory,
                audioStreamerFactory = { AudioStreamer(audioSourceFactory(), clock, Dispatchers.IO) },
                stillCapture = BurstStillCapture(
                    backend = session.backend,
                    sessionId = session.session.sessionId,
                    camera = stillCamera,
                    feedback = captureFeedback,
                    uploads = captureUploads,
                    clock = clock,
                    scope = uploadScope,
                ),
                spooling = CaptureSpooling(
                    audio = spools.audio(session.session.sessionId),
                    events = spools.events(session.session.sessionId),
                    nearCap = spools.budget.nearCap,
                    finisher = sessionFinisher,
                    loopContext = ioContext,
                ),
            )
        }
    }
}
