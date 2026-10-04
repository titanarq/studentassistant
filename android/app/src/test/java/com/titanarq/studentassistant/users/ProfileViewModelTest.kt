package com.titanarq.studentassistant.users

import com.titanarq.studentassistant.MainDispatcherRule
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.backend.FakeBackendClient
import com.titanarq.studentassistant.backend.PairedBackend
import com.titanarq.studentassistant.protocol.USER_NAME_MAX_CHARS
import com.titanarq.studentassistant.protocol.User
import com.titanarq.studentassistant.protocol.UserUpdateRequest
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class ProfileViewModelTest {
    @get:Rule
    val main = MainDispatcherRule()

    @get:Rule
    val folder = TemporaryFolder()

    @OptIn(ExperimentalCoroutinesApi::class)
    private val scope = CoroutineScope(UnconfinedTestDispatcher() + SupervisorJob())
    private val client = FakeBackendClient()
    private val store by lazy { BackendStore.create(File(folder.root, BackendStore.FILE_NAME), scope) }
    private val holder = UserHolder()
    private val photos by lazy { UserPhotos(client, store) }
    private val home = PairedBackend("http://192.168.1.20:8000", "d1", "sa_tok", "192.168.1.20:8000")
    private val laura = User("laura-mendez", "Laura Méndez", email = "laura@example.com", photoUrl = "/api/users/laura-mendez/photo")

    private fun viewModel(user: User? = laura): ProfileViewModel {
        runBlocking { store.save(home) }
        user?.let { holder.select(it) }
        return ProfileViewModel(client, store, holder, photos)
    }

    @After
    fun tearDown() {
        scope.cancel()
    }

    @Test
    fun `the form starts with the saved name and email`() {
        val vm = viewModel()

        assertEquals("Laura Méndez", vm.state.value.name)
        assertEquals("laura@example.com", vm.state.value.email)
    }

    @Test
    fun `validation follows the protocol rules`() {
        assertEquals(ProfileFieldError.NAME_REQUIRED, validateProfile("   ", "").name)
        assertEquals(ProfileFieldError.NAME_TOO_LONG, validateProfile("x".repeat(USER_NAME_MAX_CHARS + 1), "").name)
        assertNull(validateProfile("x".repeat(USER_NAME_MAX_CHARS), "").name)
        assertNull(validateProfile("Ana", "").email)
        assertEquals(ProfileFieldError.EMAIL_INVALID, validateProfile("Ana", "ana@").email)
        assertEquals(ProfileFieldError.EMAIL_INVALID, validateProfile("Ana", "a b@c.d").email)
        assertEquals(ProfileFieldError.EMAIL_TOO_LONG, validateProfile("Ana", "a".repeat(250) + "@b.cd").email)
        assertTrue(validateProfile(" Ana ", " ana@x.es ").ok)
    }

    @Test
    fun `an invalid form sends nothing and shows the problems`() {
        val vm = viewModel()
        vm.onNameChange("  ")
        vm.onEmailChange("nope")

        vm.save()

        assertEquals(emptyList<String>(), client.calls)
        assertEquals(ProfileFieldError.NAME_REQUIRED, vm.state.value.validation.name)
        assertEquals(ProfileFieldError.EMAIL_INVALID, vm.state.value.validation.email)
        vm.onNameChange("Laura")
        assertNull(vm.state.value.validation.name)
    }

    @Test
    fun `only the changed fields are sent and the holder gets the answer`() {
        val vm = viewModel()
        val updated = laura.copy(name = "Laura M.")
        client.updateUserResult = BackendResult.Success(updated)
        vm.onNameChange("  Laura M.  ")

        vm.save()

        assertEquals(listOf(UserUpdateRequest(name = "Laura M.")), client.userUpdates)
        assertEquals("updateUser http://192.168.1.20:8000 laura-mendez", client.calls.single())
        assertEquals(updated, holder.current.value)
        assertTrue(vm.state.value.saved)
        assertFalse(vm.state.value.busy)
    }

    @Test
    fun `an emptied email is sent as the empty string`() {
        val vm = viewModel()
        client.updateUserResult = BackendResult.Success(laura.copy(email = null))
        vm.onEmailChange("")

        vm.save()

        assertEquals(listOf(UserUpdateRequest(email = "")), client.userUpdates)
        assertNull(holder.current.value?.email)
    }

    @Test
    fun `nothing changed sends nothing and finishes`() {
        val vm = viewModel()

        vm.save()

        assertEquals(emptyList<String>(), client.calls)
        assertTrue(vm.state.value.saved)
    }

    @Test
    fun `a 422 shows the rejection and keeps the form`() {
        val vm = viewModel()
        client.updateUserResult = BackendResult.HttpError(422)
        vm.onNameChange("Otra")

        vm.save()

        assertTrue(vm.state.value.rejected)
        assertNull(vm.state.value.failure)
        assertFalse(vm.state.value.saved)
        assertEquals("Otra", vm.state.value.name)
        assertEquals(laura, holder.current.value)
    }

    @Test
    fun `a network failure is shown and the holder is unchanged`() {
        val vm = viewModel()
        client.updateUserResult = BackendResult.Unreachable("timeout")
        vm.onNameChange("Otra")

        vm.save()

        assertEquals(BackendResult.Unreachable("timeout"), vm.state.value.failure)
        assertFalse(vm.state.value.rejected)
        assertFalse(vm.state.value.busy)
        assertEquals(laura, holder.current.value)
    }

    @Test
    fun `a picked photo is uploaded as JPEG and replaces the user`() {
        val vm = viewModel()
        val updated = laura.copy(photoUrl = "/api/users/laura-mendez/photo?v=2")
        client.putUserPhotoResult = BackendResult.Success(updated)
        val before = photos.version.value

        vm.uploadPhoto { PreparedPhoto(byteArrayOf(9, 8, 7)) }

        val (bytes, type) = client.userPhotoUploads.single()
        assertEquals(listOf<Byte>(9, 8, 7), bytes.toList())
        assertEquals("image/jpeg", type)
        assertEquals(updated, holder.current.value)
        assertEquals(before + 1, photos.version.value)
        assertFalse(vm.state.value.busy)
        assertFalse(vm.state.value.saved)
    }

    @Test
    fun `an unreadable or oversized photo is never sent`() {
        val vm = viewModel()

        vm.uploadPhoto { null }
        assertTrue(vm.state.value.photoUnreadable)

        vm.uploadPhoto { PreparedPhoto(ByteArray(PHOTO_MAX_BYTES + 1)) }
        assertTrue(vm.state.value.photoUnreadable)

        assertEquals(emptyList<String>(), client.calls)
        assertFalse(vm.state.value.busy)
    }

    @Test
    fun `a failed upload shows the failure`() {
        val vm = viewModel()
        client.putUserPhotoResult = BackendResult.HttpError(413)

        vm.uploadPhoto { PreparedPhoto(byteArrayOf(1)) }

        assertEquals(BackendResult.HttpError(413), vm.state.value.failure)
        assertEquals(laura, holder.current.value)
    }

    @Test
    fun `removing the photo sends DELETE and updates the holder`() {
        val vm = viewModel()
        val updated = laura.copy(photoUrl = null)
        client.deleteUserPhotoResult = BackendResult.Success(updated)
        val before = photos.version.value

        vm.removePhoto()

        assertEquals("deleteUserPhoto http://192.168.1.20:8000 laura-mendez", client.calls.single())
        assertNull(holder.current.value?.photoUrl)
        assertEquals(before + 1, photos.version.value)
    }

    @Test
    fun `no selected user does nothing`() {
        val vm = viewModel(user = null)

        vm.save()
        vm.removePhoto()

        assertEquals(emptyList<String>(), client.calls)
    }
}
