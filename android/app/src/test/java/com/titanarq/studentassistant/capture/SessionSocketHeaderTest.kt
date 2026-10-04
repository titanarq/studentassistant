package com.titanarq.studentassistant.capture

import com.titanarq.studentassistant.protocol.USER_HEADER
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.util.concurrent.TimeUnit

/** The WebSocket handshake carries `X-SA-User` when there is a user (protocol 1.8, #554). */
class SessionSocketHeaderTest {
    private val server = MockWebServer()
    private val silent = object : SessionSocketListener {
        override fun onOpen() = Unit
        override fun onText(text: String) = Unit
        override fun onClosed(code: Int, reason: String) = Unit
        override fun onFailure(reason: String) = Unit
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun handshake(userId: String?): okhttp3.mockwebserver.RecordedRequest {
        server.start()
        server.enqueue(
            MockResponse().withWebSocketUpgrade(
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) = Unit
                },
            ),
        )
        val socket = OkHttpSessionSocketFactory().open(server.url("/ws/sessions/s1").toString(), "sa_tok", userId, silent)
        val request = server.takeRequest(5, TimeUnit.SECONDS)!!
        socket.close()
        return request
    }

    @Test
    fun `the handshake names the user next to the bearer token`() {
        val request = handshake("laura-mendez")

        assertEquals("laura-mendez", request.getHeader(USER_HEADER))
        assertEquals("Bearer sa_tok", request.getHeader("Authorization"))
    }

    @Test
    fun `without a user the handshake has no user header`() {
        assertNull(handshake(null).getHeader(USER_HEADER))
    }
}
