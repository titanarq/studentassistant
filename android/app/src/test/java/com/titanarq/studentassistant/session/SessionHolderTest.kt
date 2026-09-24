package com.titanarq.studentassistant.session

import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionActiveStatus
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SessionHolderTest {
    @Test
    fun `it holds the last opened session until cleared`() {
        val holder = SessionHolder()
        assertNull(holder.current.value)
        val open = OpenSession(
            backend = BackendCredentials("http://192.168.1.20:8000", "sa_tok"),
            session = Session("s1", "historia", "feudalismo", SessionActiveStatus.ACTIVE, 1, "/ws/sessions/s1", "1.0"),
            subjectName = "Historia",
            topicName = "El feudalismo",
        )

        holder.open(open)
        assertEquals(open, holder.current.value)

        holder.clear()
        assertNull(holder.current.value)
    }
}
