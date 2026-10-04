package com.titanarq.studentassistant.users

import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.protocol.User
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The profile photos of the active backend's users: `GET` of the user's `photo_url` with the
 * bearer token, kept in memory by URL (never on disk). A user without a photo, or a photo that
 * cannot be read, is null and the screens show the initials instead.
 */
class UserPhotos(
    private val client: BackendClient,
    private val store: BackendStore,
) {
    private val cache = HashMap<String, ByteArray>()
    private val _version = MutableStateFlow(0)

    /** Grows every time the kept photos are forgotten, so an avatar draws its photo again (#555). */
    val version: StateFlow<Int> = _version.asStateFlow()

    /** The photo's image bytes, or null. */
    suspend fun load(user: User): ByteArray? {
        val photoUrl = user.photoUrl ?: return null
        val active = store.active() ?: return null
        val key = active.baseUrl + photoUrl
        synchronized(cache) { cache[key] }?.let { return it }
        return when (val result = client.userPhoto(active.credentials, photoUrl)) {
            is BackendResult.Success -> result.value.also { synchronized(cache) { cache[key] = it } }
            is BackendResult.Failure -> null
        }
    }

    /** Forgets every kept photo (a user changed theirs, #555). */
    fun invalidate() {
        synchronized(cache) { cache.clear() }
        _version.value += 1
    }
}

/** The one or two capital letters that stand for [name] when there is no photo. */
fun initialsOf(name: String): String {
    val words = name.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }
    return when {
        words.isEmpty() -> "?"
        words.size == 1 -> words[0].take(1).uppercase()
        else -> (words.first().take(1) + words.last().take(1)).uppercase()
    }
}
