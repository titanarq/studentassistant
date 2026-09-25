package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.backend.defaultOkHttpClient
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString

/** One open (or opening) session WebSocket. Sends never block; false means it was not queued. */
interface SessionSocket {
    fun sendText(text: String): Boolean

    fun sendBinary(bytes: ByteArray): Boolean

    /** Bytes accepted by the send methods and not transmitted yet. */
    fun queuedBytes(): Long = 0

    /** Closes normally; the listener then hears nothing more from this socket. */
    fun close()
}

/** What a [SessionSocket] reports, from any thread. Exactly one of the two endings is reported. */
interface SessionSocketListener {
    fun onOpen()

    fun onText(text: String)

    /** The peer closed the socket with [code] and [reason]. */
    fun onClosed(code: Int, reason: String)

    /** The connection could not be opened or broke (no close frame). */
    fun onFailure(reason: String)
}

/** Opens the session WebSocket at [url] (http(s) or ws(s)) with the bearer [token]. */
fun interface SessionSocketFactory {
    fun open(url: String, token: String, listener: SessionSocketListener): SessionSocket
}

/** The [OkHttpClient] of session sockets: no read timeout, pings every 10 s to notice a dead LAN. */
fun sessionSocketHttpClient(): OkHttpClient =
    defaultOkHttpClient().newBuilder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(10, TimeUnit.SECONDS)
        .build()

/** [SessionSocketFactory] over OkHttp's WebSocket; the token travels as `Authorization: Bearer`. */
class OkHttpSessionSocketFactory(
    private val http: OkHttpClient = sessionSocketHttpClient(),
) : SessionSocketFactory {
    override fun open(url: String, token: String, listener: SessionSocketListener): SessionSocket {
        val request = Request.Builder()
            .url(url)
            .header("Authorization", "Bearer $token")
            .build()
        val socket = http.newWebSocket(request, Listener(listener))
        return object : SessionSocket {
            override fun sendText(text: String): Boolean = socket.send(text)

            override fun sendBinary(bytes: ByteArray): Boolean = socket.send(bytes.toByteString())

            override fun queuedBytes(): Long = socket.queueSize()

            override fun close() {
                socket.close(NORMAL_CLOSURE, null)
            }
        }
    }

    private class Listener(private val listener: SessionSocketListener) : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) = listener.onOpen()

        override fun onMessage(webSocket: WebSocket, text: String) = listener.onText(text)

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            // The backend sends no binary messages; one would be a contract violation.
            webSocket.close(PROTOCOL_ERROR, "unexpected binary message")
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, null)
            listener.onClosed(code, reason)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            // Never the token: only the exception's class and message (OkHttp's carry no headers).
            listener.onFailure(response?.let { "HTTP ${it.code}" } ?: (t.message ?: t::class.java.simpleName))
        }
    }

    private companion object {
        const val NORMAL_CLOSURE = 1000
        const val PROTOCOL_ERROR = 1002
    }
}
