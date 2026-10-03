package com.titanarq.studentassistant.protocol

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The users bodies of protocol 1.8 (#545). [ProtocolExamplesTest] round-trips the five shared
 * examples; this pins what this side refuses, field by field, exactly as the schemas and the Python
 * models do.
 */
class UsersTest {
    private val laura = User("laura-mendez", "Laura Méndez")
    private val name = JsonPrimitive("Laura Méndez")

    /** A minimal valid `User` body, with [overrides] written over it (a [JsonNull] value included). */
    private fun user(vararg overrides: Pair<String, JsonElement>): JsonObject {
        val fields: MutableMap<String, JsonElement> =
            linkedMapOf("id" to JsonPrimitive("laura-mendez"), "name" to name)
        fields.putAll(overrides.toMap())
        return JsonObject(fields)
    }

    private fun usersList(vararg users: JsonElement): JsonObject =
        JsonObject(mapOf("users" to JsonArray(users.toList())))

    private fun decode(message: String, body: JsonElement): Any = codecFor(message).decode(body)

    /** The contract is refused: an unknown field by the codec, a broken rule by the class. */
    private fun refused(message: String, body: JsonElement) {
        val failure = assertThrows(Exception::class.java) { decode(message, body) }
        assertTrue(
            "$message: expected a contract violation, got ${failure::class.simpleName}: ${failure.message}",
            failure is SerializationException || failure is IllegalArgumentException,
        )
    }

    @Test
    fun `the shared list example decodes into its users`() {
        val body = Json.parseToJsonElement(SharedExamples.read("rest.users.list.response"))
        assertEquals(
            UsersListResponse(
                listOf(
                    User("laura-mendez", "Laura Méndez", "laura.mendez@example.com", "/api/users/laura-mendez/photo"),
                    User("diego-soler", "Diego Soler"),
                ),
            ),
            decode("rest.users.list.response", body),
        )
    }

    @Test
    fun `an unknown field is refused, in a User of either response and in the list itself`() {
        val tampered = user("role" to JsonPrimitive("admin"))
        refused("rest.users.create.response", tampered)
        refused("rest.users.update.response", tampered)
        refused("rest.users.list.response", usersList(tampered))
        refused("rest.users.list.response", JsonObject(usersList() + ("total" to JsonPrimitive(0))))
    }

    @Test
    fun `a blank name is refused everywhere a name is carried`() {
        for (blank in listOf("", " ", "   ", "\t")) {
            val field = JsonPrimitive(blank)
            refused("rest.users.create.request", JsonObject(mapOf("name" to field)))
            refused("rest.users.update.request", JsonObject(mapOf("name" to field)))
            refused("rest.users.create.response", user("name" to field))
            refused("rest.users.list.response", usersList(user("name" to field)))
        }
    }

    @Test
    fun `an untrimmed name is refused, because a name travels trimmed`() {
        for (untrimmed in listOf(" Laura", "Laura ", " Laura Méndez ")) {
            refused("rest.users.create.response", user("name" to JsonPrimitive(untrimmed)))
        }
    }

    @Test
    fun `a name is bounded to USER_NAME_MAX_CHARS`() {
        val longest = "a".repeat(USER_NAME_MAX_CHARS)
        val taken = decode("rest.users.create.response", user("name" to JsonPrimitive(longest))) as User
        assertEquals(longest, taken.name)
        refused("rest.users.create.response", user("name" to JsonPrimitive("${longest}a")))
    }

    @Test
    fun `an email is bounded to USER_EMAIL_MAX_CHARS`() {
        val longest = "${"a".repeat(USER_EMAIL_MAX_CHARS - "@example.com".length)}@example.com"
        assertEquals(USER_EMAIL_MAX_CHARS, longest.length)
        val taken = decode("rest.users.create.response", user("email" to JsonPrimitive(longest))) as User
        assertEquals(longest, taken.email)
        refused("rest.users.create.response", user("email" to JsonPrimitive("a$longest")))
    }

    @Test
    fun `a malformed email is refused in a response and in a create request`() {
        val malformed = listOf("laura@example", "laura.example.com", "laura@ example.com", "laura@@example.com", "@example.com")
        for (email in malformed) {
            val field = JsonPrimitive(email)
            refused("rest.users.create.response", user("email" to field))
            refused("rest.users.create.request", JsonObject(mapOf("name" to name, "email" to field)))
        }
    }

    @Test
    fun `an empty email is taken only in the update request, where it clears the one the user has`() {
        assertEquals(
            UserUpdateRequest(email = ""),
            decode("rest.users.update.request", JsonObject(mapOf("email" to JsonPrimitive("")))),
        )
        refused("rest.users.create.request", JsonObject(mapOf("name" to name, "email" to JsonPrimitive(""))))
        refused("rest.users.create.response", user("email" to JsonPrimitive("")))
        refused("rest.users.list.response", usersList(user("email" to JsonPrimitive(""))))
    }

    @Test
    fun `an update carrying neither field is refused, and either one on its own is taken`() {
        val nothing = assertThrows(IllegalArgumentException::class.java) {
            decode("rest.users.update.request", JsonObject(emptyMap()))
        }
        val why = checkNotNull(nothing.message)
        assertTrue(why, why.contains("at least one"))
        assertEquals(
            UserUpdateRequest(name = "Laura Méndez Ruiz"),
            decode("rest.users.update.request", JsonObject(mapOf("name" to JsonPrimitive("Laura Méndez Ruiz")))),
        )
        assertEquals(
            UserUpdateRequest(email = "laura.mendez@example.com"),
            decode("rest.users.update.request", JsonObject(mapOf("email" to JsonPrimitive("laura.mendez@example.com")))),
        )
    }

    @Test
    fun `a photo_url of the users API is taken and any other path is refused`() {
        for (photoUrl in listOf("/api/users/laura-mendez/photo", "/api/users/laura-mendez")) {
            val taken = decode("rest.users.create.response", user("photo_url" to JsonPrimitive(photoUrl))) as User
            assertEquals(photoUrl, taken.photoUrl)
        }
        val elsewhere = listOf("https://example.com/photo.jpg", "/api/subjects/biologia", "api/users/laura-mendez/photo")
        for (photoUrl in elsewhere) {
            refused("rest.users.create.response", user("photo_url" to JsonPrimitive(photoUrl)))
        }
    }

    @Test
    fun `a user id is a slug like every other id`() {
        for (userId in listOf("laura-mendez", "laura-mendez-2", "L2")) {
            val taken = decode("rest.users.create.response", user("id" to JsonPrimitive(userId))) as User
            assertEquals(userId, taken.id)
        }
        for (notASlug in listOf("Laura Méndez", "-laura", "laura.mendez", "")) {
            refused("rest.users.create.response", user("id" to JsonPrimitive(notASlug)))
        }
    }

    @Test
    fun `an absent optional stays absent when the body is encoded again`() {
        val codec = codecFor("rest.users.create.response")
        val decoded = codec.decode(user())
        assertEquals(laura, decoded)
        assertEquals(user(), codec.encode(decoded))
        assertEquals("""{"id":"laura-mendez","name":"Laura Méndez"}""", ProtocolJson.encodeToString(User.serializer(), laura))
    }

    @Test
    fun `an explicit null optional reads as absent, the way ProtocolJson carries every optional`() {
        // `explicitNulls = false` is this package's one codec, so a null the backend never sends
        // reads as "not there" where the web's own decoder refuses it outright.
        val codec = codecFor("rest.users.update.response")
        assertEquals(laura, codec.decode(user("email" to JsonNull, "photo_url" to JsonNull)))
        assertEquals(user(), codec.encode(codec.decode(user("email" to JsonNull))))
    }

    @Test
    fun `the active-user rule names its header, its cookie and the photo types`() {
        assertEquals("X-SA-User", USER_HEADER)
        assertEquals("sa_user", USER_COOKIE)
        assertEquals(80, USER_NAME_MAX_CHARS)
        assertEquals(254, USER_EMAIL_MAX_CHARS)
        assertEquals(listOf("image/jpeg", "image/png", "image/webp"), USER_PHOTO_CONTENT_TYPES)
    }
}
