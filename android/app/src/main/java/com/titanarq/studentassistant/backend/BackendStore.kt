package com.titanarq.studentassistant.backend

import androidx.datastore.core.CorruptionException
import androidx.datastore.core.DataStore
import androidx.datastore.core.DataStoreFactory
import androidx.datastore.core.Serializer
import androidx.datastore.core.handlers.ReplaceFileCorruptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import java.io.File
import java.io.InputStream
import java.io.OutputStream

/** One backend this phone paired with. The [token] never appears in [toString]. */
@Serializable
data class PairedBackend(
    /** `http://host:port`, no trailing slash. */
    @SerialName("base_url") val baseUrl: String,
    @SerialName("device_id") val deviceId: String,
    val token: String,
    /** What the list shows; the URL's `host:port` unless the student names it. */
    @SerialName("display_name") val displayName: String,
) {
    val credentials: BackendCredentials get() = BackendCredentials(baseUrl, token)

    override fun toString(): String =
        "PairedBackend(baseUrl=$baseUrl, deviceId=$deviceId, token=<redacted>, displayName=$displayName)"
}

/** Everything the store holds: the paired backends and which one is active. */
@Serializable
data class PairedBackends(
    val backends: List<PairedBackend> = emptyList(),
    @SerialName("active_device_id") val activeDeviceId: String? = null,
) {
    /** The backend the app talks to, if any. */
    val active: PairedBackend? get() = backends.firstOrNull { it.deviceId == activeDeviceId }
}

/** The on-disk form of [PairedBackends]: JSON, in the app's private files directory. */
object PairedBackendsSerializer : Serializer<PairedBackends> {
    private val json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    override val defaultValue: PairedBackends = PairedBackends()

    override suspend fun readFrom(input: InputStream): PairedBackends =
        try {
            json.decodeFromString(PairedBackends.serializer(), input.readBytes().decodeToString())
        } catch (e: SerializationException) {
            throw CorruptionException("unreadable paired backends file", e)
        } catch (e: IllegalArgumentException) {
            throw CorruptionException("unreadable paired backends file", e)
        }

    override suspend fun writeTo(t: PairedBackends, output: OutputStream) {
        output.write(json.encodeToString(PairedBackends.serializer(), t).encodeToByteArray())
    }
}

/**
 * The paired backends, persisted with Jetpack DataStore: several can be stored, one is active,
 * any can be removed. Tokens rest only in the app-private DataStore file (backup is disabled in
 * the manifest) and are never logged.
 */
class BackendStore(private val dataStore: DataStore<PairedBackends>) {
    /** The stored backends, re-emitted on every change. */
    val backends: Flow<PairedBackends> = dataStore.data

    /** The current snapshot. */
    suspend fun current(): PairedBackends = dataStore.data.first()

    /** The active backend, if any. */
    suspend fun active(): PairedBackend? = current().active

    /**
     * Stores [backend] and makes it the active one. Pairing again with a backend already stored
     * (same base URL or same device id) replaces that entry instead of adding a second one.
     */
    suspend fun save(backend: PairedBackend) {
        dataStore.updateData { stored ->
            val others = stored.backends.filterNot {
                it.baseUrl == backend.baseUrl || it.deviceId == backend.deviceId
            }
            PairedBackends(backends = others + backend, activeDeviceId = backend.deviceId)
        }
    }

    /** Makes the stored backend [deviceId] the active one; an unknown id changes nothing. */
    suspend fun setActive(deviceId: String) {
        dataStore.updateData { stored ->
            if (stored.backends.none { it.deviceId == deviceId }) stored else stored.copy(activeDeviceId = deviceId)
        }
    }

    /** Forgets backend [deviceId]; when it was the active one, the first remaining becomes active. */
    suspend fun remove(deviceId: String) {
        dataStore.updateData { stored ->
            val remaining = stored.backends.filterNot { it.deviceId == deviceId }
            val active = stored.activeDeviceId.takeIf { id -> remaining.any { it.deviceId == id } }
                ?: remaining.firstOrNull()?.deviceId
            PairedBackends(remaining, active)
        }
    }

    companion object {
        /** The DataStore file's name inside the app's files directory. */
        const val FILE_NAME = "paired_backends.json"

        /**
         * A store backed by [file]. A corrupt file is replaced by an empty store (the student
         * pairs again) instead of crashing the app. Only one store may exist per file.
         */
        fun create(
            file: File,
            scope: CoroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob()),
        ): BackendStore = BackendStore(
            DataStoreFactory.create(
                serializer = PairedBackendsSerializer,
                corruptionHandler = ReplaceFileCorruptionHandler { PairedBackends() },
                scope = scope,
                produceFile = { file },
            ),
        )
    }
}
