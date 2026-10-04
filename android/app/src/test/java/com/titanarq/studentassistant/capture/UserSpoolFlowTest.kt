@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.CaptureImageBytes
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.CaptureTrigger
import com.titanarq.studentassistant.protocol.CaptureUploadResponse
import com.titanarq.studentassistant.protocol.CaptureUploadStatus
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionActiveStatus
import com.titanarq.studentassistant.protocol.SessionEndResponse
import com.titanarq.studentassistant.protocol.SessionEndedStatus
import com.titanarq.studentassistant.spool.CaptureSpool
import com.titanarq.studentassistant.spool.PendingEnd
import com.titanarq.studentassistant.spool.SpoolBudget
import com.titanarq.studentassistant.spool.Spools
import java.io.File
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Spooled captures and ends are sent as their own user, not the one selected at restart (#554). */
class UserSpoolFlowTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val plain = BackendCredentials("http://pc:8000", "sa_tok")
    private val stills = listOf(Still(CaptureImageBytes(byteArrayOf(1)), "image/jpeg", 10, 10, 1_010))

    @Test
    fun `a restored capture uploads as the user who took it, one from before 1_8 with no user`() = runTest {
        val spool = CaptureSpool(File(folder.root, "captures"), SpoolBudget(1_000_000))
        val first = CaptureUploadQueue(backgroundScope, ScriptedUploadClient(BackendResult.Unreachable("offline")), listOf(1_000), 10, spool)
        first.begin("c-1", plain.forUser("laura-mendez"), "s1", CaptureTrigger.BUTTON, null, 1_000)
        first.submit("c-1", Burst(stills, null))
        first.begin("c-2", plain, "s1", CaptureTrigger.BUTTON, null, 2_000)
        first.submit("c-2", Burst(stills, null))
        runCurrent()

        val client = ScriptedUploadClient(BackendResult.Success(CaptureUploadResponse("c-1", "s1", CaptureUploadStatus.STORED, 1, 5_000)))
        val restarted = CaptureUploadQueue(backgroundScope, client, listOf(1_000), 10, spool)
        // The credentials lookup knows no user: each capture brings its own.
        restarted.restore { plain }
        runCurrent()

        assertEquals(listOf("c-1", "c-2"), client.uploads.map { it.metadata.captureId })
        assertEquals(listOf<String?>("laura-mendez", null), client.uploads.map { it.backend.userId })
    }

    @Test
    fun `a pending end goes out as its own user after a restart`() = runTest {
        val root = File(folder.root, "spool")
        val spools = Spools(root, SpoolBudget(10_000_000))
        spools.putEnd(PendingEnd("s1", plain.baseUrl, 9_000, userId = "laura-mendez"))
        spools.putEnd(PendingEnd("s2", plain.baseUrl, 9_000))
        val client = FakeBackendClient()
        client.resumeSessionResult = BackendResult.Success(
            Session("s1", "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/s1", "1.8"),
        )
        client.endSessionResult = BackendResult.Success(SessionEndResponse("s1", SessionEndedStatus.ENDED, 10_000))
        val finisher = SessionFinisher(
            scope = backgroundScope,
            client = client,
            spools = spools,
            uploads = CaptureUploadQueue(backgroundScope, client, listOf(1_000), 10, spools.captures),
            socketFactory = FakeSessionSocketFactory(),
            clock = FakeClock(),
            credentials = { plain }, // knows no user
            retryDelaysMs = listOf(1_000),
        )

        finisher.restore()
        runCurrent()

        val ends = client.calls.zip(client.userIds).filter { it.first.startsWith("endSession") }
        assertEquals(setOf<String?>("laura-mendez", null), ends.map { it.second }.toSet())
        assertEquals(2, ends.size)
        assertNull(client.userIds.firstOrNull { it == "pablo" })
    }
}
