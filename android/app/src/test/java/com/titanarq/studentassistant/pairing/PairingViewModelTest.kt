package com.titanarq.studentassistant.pairing

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.backend.PairedBackend
import com.titanarq.studentassistant.protocol.ClientKind
import com.titanarq.studentassistant.protocol.PROTOCOL_VERSION
import com.titanarq.studentassistant.protocol.PairRequest
import com.titanarq.studentassistant.protocol.PairResponse
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class PairingViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    // The store runs on the test thread: store work on Dispatchers.IO could outlive the test and
    // resume a view model on Dispatchers.Main after the rule reset it, failing a later test.
    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val client = FakeBackendClient()
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val viewModel by lazy { PairingViewModel(client, store, deviceName = "Pixel 8") }

    private val qr = """{"url":"http://192.168.1.20:8000","code":"ABCD-EFGH"}"""

    @After
    fun tearDown() {
        scope.cancel()
    }

    /** The view model's state once it is no longer [PairingUiState.Pairing]. */
    private fun settled(): PairingUiState = runBlocking {
        viewModel.state.first { it != PairingUiState.Pairing }
    }

    @Test
    fun `a scanned QR pairs as an android client and stores the backend as active`() {
        client.pairResult = BackendResult.Success(PairResponse("d1", "sa_tok", "1.0"))

        viewModel.onQrScanned(qr)

        assertEquals(PairingUiState.Paired("192.168.1.20:8000"), settled())
        assertEquals(listOf("pair http://192.168.1.20:8000 ABCD-EFGH"), client.calls)
        assertEquals(PairRequest("ABCD-EFGH", "Pixel 8", ClientKind.ANDROID, PROTOCOL_VERSION), client.lastPairRequest)
        assertEquals(
            PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "192.168.1.20:8000"),
            runBlocking { store.active() },
        )
    }

    @Test
    fun `the manual form goes through the same pairing path`() {
        client.pairResult = BackendResult.Success(PairResponse("d1", "sa_tok", "1.0"))

        viewModel.onManualEntry("192.168.1.20:8000", "abcd-efgh")

        assertEquals(PairingUiState.Paired("192.168.1.20:8000"), settled())
        assertEquals(listOf("pair http://192.168.1.20:8000 ABCD-EFGH"), client.calls)
        assertEquals("d1", runBlocking { store.active() }?.deviceId)
    }

    @Test
    fun `a malformed QR fails with a payload error without calling the backend`() {
        viewModel.onQrScanned("https://example.org")

        assertEquals(PairingUiState.Failed(PairingFailure.InvalidPayload(PairingPayloadError.NOT_A_PAIRING_QR)), settled())
        assertTrue(client.calls.isEmpty())
    }

    @Test
    fun `a rejected code fails and stores nothing`() {
        client.pairResult = BackendResult.HttpError(401)

        viewModel.onQrScanned(qr)

        assertEquals(PairingUiState.Failed(PairingFailure.CodeRejected), settled())
        assertNull(runBlocking { store.active() })
    }

    @Test
    fun `another HTTP error keeps its status`() {
        client.pairResult = BackendResult.HttpError(500)

        viewModel.onQrScanned(qr)

        assertEquals(PairingUiState.Failed(PairingFailure.HttpError(500)), settled())
    }

    @Test
    fun `an incompatible MAJOR version is refused and nothing is stored`() {
        client.pairResult = BackendResult.Success(PairResponse("d1", "sa_tok", "2.0"))

        viewModel.onQrScanned(qr)

        assertEquals(PairingUiState.Failed(PairingFailure.IncompatibleVersion("2.0", PROTOCOL_VERSION)), settled())
        assertNull(runBlocking { store.active() })
    }

    @Test
    fun `an incompatible version reported by the client is refused too`() {
        client.pairResult = BackendResult.IncompatibleVersion("3.0", "1.0")

        viewModel.onQrScanned(qr)

        assertEquals(PairingUiState.Failed(PairingFailure.IncompatibleVersion("3.0", "1.0")), settled())
    }

    @Test
    fun `an unreachable backend fails`() {
        client.pairResult = BackendResult.Unreachable("ConnectException")

        viewModel.onQrScanned(qr)

        assertEquals(PairingUiState.Failed(PairingFailure.Unreachable), settled())
        assertNull(runBlocking { store.active() })
    }

    @Test
    fun `an invalid answer fails`() {
        client.pairResult = BackendResult.InvalidResponse("bad")

        viewModel.onQrScanned(qr)

        assertEquals(PairingUiState.Failed(PairingFailure.InvalidResponse), settled())
    }

    @Test
    fun `after a failure the same QR is not retried in a loop until reset`() {
        client.pairResult = BackendResult.HttpError(401)
        viewModel.onQrScanned(qr)
        settled()

        viewModel.onQrScanned(qr)
        viewModel.onQrScanned(qr)
        assertEquals(1, client.calls.size)

        viewModel.reset()
        assertEquals(PairingUiState.Idle, viewModel.state.value)
        client.pairResult = BackendResult.Success(PairResponse("d1", "sa_tok", "1.0"))
        viewModel.onQrScanned(qr)
        assertEquals(PairingUiState.Paired("192.168.1.20:8000"), settled())
        assertEquals(2, client.calls.size)
    }

    @Test
    fun `the manual form retries straight after a failure`() {
        client.pairResult = BackendResult.Unreachable("x")
        viewModel.onQrScanned(qr)
        settled()

        client.pairResult = BackendResult.Success(PairResponse("d1", "sa_tok", "1.0"))
        viewModel.onManualEntry("http://192.168.1.20:8000", "ABCD-EFGH")

        assertEquals(PairingUiState.Paired("192.168.1.20:8000"), settled())
    }

    @Test
    fun `display names drop the default port`() {
        assertEquals("mypc.local", PairingViewModel.displayNameOf("http://mypc.local"))
        assertEquals("mypc.local", PairingViewModel.displayNameOf("https://mypc.local"))
        assertEquals("10.0.0.2:8000", PairingViewModel.displayNameOf("http://10.0.0.2:8000"))
    }
}
