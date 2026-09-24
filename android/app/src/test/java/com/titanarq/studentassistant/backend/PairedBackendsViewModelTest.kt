package com.titanarq.studentassistant.backend

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.ui.Route
import com.titanarq.studentassistant.ui.startRoute
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class PairedBackendsViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }

    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_1", "192.168.1.20:8000")
    private val lab = PairedBackend("http://10.0.0.5:8000", "d2", "sa_2", "10.0.0.5:8000")

    @After
    fun tearDown() {
        scope.cancel()
    }

    @Test
    fun `lists the stored backends, switches the active one and removes one`() = runBlocking {
        store.save(home)
        store.save(lab)
        val viewModel = PairedBackendsViewModel(store)

        assertEquals(PairedBackends(listOf(home, lab), "d2"), viewModel.backends.first { it != null })

        viewModel.setActive("d1")
        assertEquals("d1", viewModel.backends.first { it?.activeDeviceId == "d1" }!!.activeDeviceId)

        viewModel.remove("d1")
        assertEquals(PairedBackends(listOf(lab), "d2"), viewModel.backends.first { it?.backends?.size == 1 })
    }

    @Test
    fun `the app opens pairing only when no backend is stored`() {
        assertEquals(Route.PAIRING, startRoute(PairedBackends()))
        assertEquals(Route.BACKENDS, startRoute(PairedBackends(listOf(home), "d1")))
    }
}
