package com.titanarq.studentassistant.spool

import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.CaptureImage
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadRequest
import com.titanarq.studentassistant.protocol.ProtocolJson
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.SubjectsListResponse
import com.titanarq.studentassistant.protocol.Topic
import com.titanarq.studentassistant.protocol.TopicsListResponse
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/** The spool keeps the user each item belongs to; items from before 1.8 have none (#554). */
class SpoolUserIdTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val root: File get() = File(folder.root, "spool")
    private val backend = BackendCredentials("http://pc:8000", "sa_tok")
    private val request = CaptureUploadRequest("c1", CaptureTrigger.BUTTON, null, 1, listOf(CaptureImage("image_0", "image/jpeg", 1, 1, 1)))

    private fun spools() = Spools(root, SpoolBudget(10_000_000))

    @Test
    fun `a spooled capture records its user and an old one without still decodes`() {
        val meta = SpooledCaptureMeta("s1", backend.baseUrl, request, userId = "laura-mendez")
        val text = ProtocolJson.encodeToString(SpooledCaptureMeta.serializer(), meta)
        assertTrue(text, text.contains(""""user_id":"laura-mendez""""))
        assertEquals(meta, ProtocolJson.decodeFromString(SpooledCaptureMeta.serializer(), text))

        val old = ProtocolJson.encodeToString(SpooledCaptureMeta.serializer(), meta.copy(userId = null))
        assertFalse(old, old.contains("user_id"))
        assertNull(ProtocolJson.decodeFromString(SpooledCaptureMeta.serializer(), old).userId)
    }

    @Test
    fun `a pending end records its user and an old one without still decodes`() {
        val end = PendingEnd("s1", backend.baseUrl, 9_000, userId = "laura-mendez")
        val spools = spools()
        spools.putEnd(end)
        assertEquals(listOf(end), spools.ends())

        File(root, "ends/s2.json").writeText("""{"session_id":"s2","base_url":"http://pc:8000","client_time_ms":1}""")
        assertNull(spools.ends().single { it.sessionId == "s2" }.userId)
    }

    @Test
    fun `bind records the backend and the user, an older session json has no user`() {
        val spools = spools()
        spools.bind("s1", backend.baseUrl, "laura-mendez")
        assertEquals(backend.baseUrl, spools.backendOf("s1"))
        assertEquals("laura-mendez", spools.userOf("s1"))

        File(root, "sessions/s2").mkdirs()
        File(root, "sessions/s2/session.json").writeText("""{"base_url":"http://pc:8000"}""")
        assertEquals(backend.baseUrl, spools.backendOf("s2"))
        assertNull(spools.userOf("s2"))
        assertNull(spools.userOf("s3"))
    }

    @Test
    fun `the sweeper reads the backend as the user the session was bound to`() = runBlocking {
        val hour = 60L * 60 * 1000
        spools().apply {
            audio("s1").append(SpooledFrame(0, 1, shortArrayOf(1)))
            audio("s1").close()
            bind("s1", backend.baseUrl, "laura-mendez")
            audio("s2").append(SpooledFrame(0, 1, shortArrayOf(1)))
            audio("s2").close()
        }
        File(root, "sessions/s2/session.json").writeText("""{"base_url":"http://pc:8000"}""")
        val spools = spools() // a new process: nothing is bound in it
        val client = FakeBackendClient()
        client.listSubjectsResult = BackendResult.Success(SubjectsListResponse(listOf(Subject("historia", "Historia"))))
        client.listTopicsResult = BackendResult.Success(TopicsListResponse("historia", listOf(Topic("t", "historia", "T"))))
        val sweeper = StaleSpoolSweeper(
            spools,
            client,
            Clock { System.currentTimeMillis() + 25 * hour },
            { listOf(backend) },
            graceMs = 24 * hour,
        )

        val swept = sweeper.sweep()

        assertEquals(setOf("s1", "s2"), swept.sessions.toSet())
        // One read per user: the bound one as them, the legacy one with no header.
        assertEquals(setOf<String?>("laura-mendez", null), client.userIds.toSet())
    }
}
