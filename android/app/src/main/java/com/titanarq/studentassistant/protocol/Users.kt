package com.titanarq.studentassistant.protocol

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

// The users of a shared vault and the active user of a request (protocol 1.8, #544).
//
// Several students share one backend and one vault, each with their own `users/<id>/` folder. The
// bodies below are what `GET /api/users`, `POST /api/users` and `PATCH /api/users/{user_id}`
// exchange; the profile photo has no JSON body (`PUT /api/users/{user_id}/photo` takes the raw
// image of one of `USER_PHOTO_CONTENT_TYPES` and answers with the updated `User`).
//
// Which user any other route acts for comes from the request itself: the `USER_HEADER` header, else
// the `USER_COOKIE` cookie (a present header wins), else -- when the vault holds exactly one user --
// that user, else a `400 user_required` refusal; an unknown id is `404 user_not_found`. The
// WebSocket handshake follows the same rule. This selects a user, it does not authenticate one: the
// bearer/loopback trust of ADR-0001 is unchanged and a device pairs with the backend, not with a
// user.

/** Request header naming the user a request acts for (this app). */
const val USER_HEADER: String = "X-SA-User"

/** Cookie naming the same user (the web page and this app's WebView). */
const val USER_COOKIE: String = "sa_user"

/** The longest [User.name]. */
const val USER_NAME_MAX_CHARS: Int = 80

/** The longest [User.email] (RFC 5321's limit for an address). */
const val USER_EMAIL_MAX_CHARS: Int = 254

/** The image types `PUT /api/users/{user_id}/photo` accepts; `GET` answers `image/jpeg`. */
val USER_PHOTO_CONTENT_TYPES: List<String> = listOf("image/jpeg", "image/png", "image/webp")

/** An opaque backend-assigned id: a slug, as every subject, topic, session and device id is. */
val ID_PATTERN: Regex = Regex("^[A-Za-z0-9][A-Za-z0-9_-]*$")

// A name travels trimmed: no whitespace at either end and at least one character that is not
// whitespace, so a blank name is impossible. A screen taking a name from a text field trims it
// before sending it (#555).
val USER_NAME_PATTERN: Regex = Regex("^\\S(?:.*\\S)?$")
val USER_EMAIL_PATTERN: Regex = Regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")
val USER_PHOTO_URL_PATTERN: Regex = Regex("^/api/users/")

internal fun requireUserId(id: String) {
    require(ID_PATTERN.matches(id)) { "user id '$id' is not a slug" }
}

internal fun requireUserName(name: String) {
    require(USER_NAME_PATTERN.matches(name)) { "user name '$name' is blank or not trimmed" }
    require(name.length <= USER_NAME_MAX_CHARS) {
        "user name of ${name.length} characters is longer than $USER_NAME_MAX_CHARS"
    }
}

internal fun requireUserEmail(email: String) {
    require(USER_EMAIL_PATTERN.matches(email)) { "user email '$email' is not an address" }
    require(email.length <= USER_EMAIL_MAX_CHARS) {
        "user email of ${email.length} characters is longer than $USER_EMAIL_MAX_CHARS"
    }
}

internal fun requireUserPhotoUrl(photoUrl: String) {
    // The pattern is the schema's own and anchors a prefix, so it is looked for, not matched whole.
    require(USER_PHOTO_URL_PATTERN.containsMatchIn(photoUrl)) {
        "user photo url '$photoUrl' is not a path of the users API"
    }
}

/**
 * A student of this vault: the body of the create and update responses, and an item of the list
 * response.
 *
 * [email] and [photoUrl] are absent on the wire, never `null`, when the user has none, which is how
 * [ProtocolJson] carries an optional field. A field breaking the rules above is an
 * [IllegalArgumentException] at construction, so decoding refuses it too.
 */
@Serializable
data class User(
    /** The slug of the name given at creation, with a numeric suffix when that slug is taken; a
     * later rename never changes it. */
    val id: String,
    /** The student's name as they write it, trimmed: 1 to [USER_NAME_MAX_CHARS] characters, not blank. */
    val name: String,
    /** Contact email; absent when the user has none. */
    val email: String? = null,
    /** Path of the profile photo, served by `GET /api/users/{user_id}/photo`; absent when there is none. */
    @SerialName("photo_url") val photoUrl: String? = null,
) {
    init {
        requireUserId(id)
        requireUserName(name)
        email?.let { requireUserEmail(it) }
        photoUrl?.let { requireUserPhotoUrl(it) }
    }
}

/** `GET /api/users`: every user of this vault. */
@Serializable
data class UsersListResponse(val users: List<User>)

/** `POST /api/users`: the name of the new user and, optionally, their email. */
@Serializable
data class UserCreateRequest(
    val name: String,
    val email: String? = null,
) {
    init {
        requireUserName(name)
        email?.let { requireUserEmail(it) }
    }
}

/**
 * `PATCH /api/users/{user_id}`: at least one of the two fields, each absent one left as it is.
 *
 * An [email] of `""` clears it (and the answered [User] then has no `email`); a new email follows
 * the same rules as a created one.
 */
@Serializable
data class UserUpdateRequest(
    val name: String? = null,
    val email: String? = null,
) {
    init {
        require(name != null || email != null) {
            "a user update carries at least one of `name` or `email`"
        }
        name?.let { requireUserName(it) }
        email?.let { if (it.isNotEmpty()) requireUserEmail(it) }
    }
}
