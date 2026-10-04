package com.titanarq.studentassistant.users

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.protocol.ErrorCode
import com.titanarq.studentassistant.protocol.User
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** A `user_required` / `user_not_found` refusal of the selected user clears the selection (#554). */
class UserRejectionTest {
    private val laura = User("laura", "Laura")
    private val pablo = User("pablo", "Pablo")
    private val holder = UserHolder().also { it.select(laura) }
    private val fake = FakeBackendClient()
    private val client = UserRejectionBackendClient(fake, holder)
    private val asLaura = BackendCredentials("http://pc:8000", "sa_tok", "laura")

    @Test
    fun `user_required clears the selected user`() = runBlocking {
        fake.listSubjectsResult = BackendResult.HttpError(400, ErrorCode.USER_REQUIRED)

        val result = client.listSubjects(asLaura)

        assertEquals(BackendResult.HttpError(400, ErrorCode.USER_REQUIRED), result)
        assertNull(holder.current.value)
    }

    @Test
    fun `user_not_found clears the selected user, whichever call it answers`() = runBlocking {

        assertEquals(laura, holder.current.value)
        fake.resumeSessionResult = BackendResult.HttpError(404, ErrorCode.USER_NOT_FOUND)
        client.resumeSession(asLaura, "s1")

        assertNull(holder.current.value)
    }

    @Test
    fun `other refusals, a code on another status and other users leave the selection alone`() = runBlocking {
        fake.listSubjectsResult = BackendResult.HttpError(404)
        client.listSubjects(asLaura)
        fake.listSubjectsResult = BackendResult.HttpError(409, ErrorCode.USER_REQUIRED)
        client.listSubjects(asLaura)
        fake.listSubjectsResult = BackendResult.HttpError(401)
        client.listSubjects(asLaura)
        // A spooled item of another user (or of none) says nothing about who is selected now.
        fake.listSubjectsResult = BackendResult.HttpError(404, ErrorCode.USER_NOT_FOUND)
        client.listSubjects(asLaura.forUser("pablo"))
        client.listSubjects(asLaura.forUser(null))

        assertEquals(laura, holder.current.value)
    }

    @Test
    fun `clearIfSelected keeps a different user`() {
        holder.select(pablo)

        holder.clearIfSelected("laura")

        assertEquals(pablo, holder.current.value)
    }
}
