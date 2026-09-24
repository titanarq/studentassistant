package com.titanarq.studentassistant.backend

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class BackendStoreTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val scopes = mutableListOf<CoroutineScope>()

    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_token-one", "192.168.1.20:8000")
    private val lab = PairedBackend("http://10.0.0.5:8000", "d2", "sa_token-two", "10.0.0.5:8000")

    /** A store on [file]; each gets its own scope, as a new process would. */
    private fun store(file: File = File(folder.root, BackendStore.FILE_NAME)): BackendStore {
        val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
        scopes += scope
        return BackendStore.create(file, scope)
    }

    @After
    fun tearDown() {
        scopes.forEach { it.cancel() }
    }

    @Test
    fun `an empty store has no backends and no active one`() = runBlocking {
        val store = store()

        assertEquals(PairedBackends(), store.current())
        assertNull(store.active())
    }

    @Test
    fun `saving backends keeps them all and makes the last one active`() = runBlocking {
        val store = store()

        store.save(home)
        store.save(lab)

        assertEquals(listOf(home, lab), store.current().backends)
        assertEquals(lab, store.active())
    }

    @Test
    fun `pairing again with the same backend replaces its entry`() = runBlocking {
        val store = store()
        store.save(home)
        store.save(lab)

        val repaired = home.copy(deviceId = "d3", token = "sa_token-three")
        store.save(repaired)

        assertEquals(listOf(lab, repaired), store.current().backends)
        assertEquals(repaired, store.active())
    }

    @Test
    fun `the active backend can be switched, an unknown id changes nothing`() = runBlocking {
        val store = store()
        store.save(home)
        store.save(lab)

        store.setActive("d1")
        assertEquals(home, store.active())

        store.setActive("nope")
        assertEquals(home, store.active())
    }

    @Test
    fun `removing the active backend activates the first remaining one`() = runBlocking {
        val store = store()
        store.save(home)
        store.save(lab)

        store.remove("d2")
        assertEquals(listOf(home), store.current().backends)
        assertEquals(home, store.active())

        store.remove("d1")
        assertEquals(PairedBackends(), store.current())
    }

    @Test
    fun `removing another backend keeps the active one`() = runBlocking {
        val store = store()
        store.save(home)
        store.save(lab)

        store.remove("d1")

        assertEquals(lab, store.active())
    }

    @Test
    fun `backends survive a restart of the app`() = runBlocking {
        val file = File(folder.root, "persist.json")
        val first = store(file)
        first.save(home)
        first.save(lab)
        first.setActive("d1")
        scopes.forEach { it.cancel() }

        val reopened = store(file)

        assertEquals(listOf(home, lab), reopened.current().backends)
        assertEquals(home, reopened.active())
    }

    @Test
    fun `a corrupt file resets to an empty store instead of crashing`() = runBlocking {
        val file = File(folder.root, "corrupt.json").apply { writeText("{not json") }

        val store = store(file)

        assertEquals(PairedBackends(), store.current())
        store.save(home)
        assertEquals(home, store.active())
    }

    @Test
    fun `the token never appears in toString`() {
        assertFalse(home.toString().contains("sa_token-one"))
        assertFalse(PairedBackends(listOf(home), "d1").toString().contains("sa_token-one"))
        assertFalse(home.credentials.toString().contains("sa_token-one"))
        assertTrue(home.credentials.token == "sa_token-one")
    }
}
