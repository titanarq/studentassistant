package com.titanarq.studentassistant.desk

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

/** The `sa_user` cookie next to `sa_token` (protocol 1.8, #554). */
class StudyDeskUserTest {
    private val topic = DeskTopic("historia", "feudalismo", "El feudalismo")

    @Test
    fun `the user cookie names the user for the whole origin`() {
        assertEquals("sa_user=laura-mendez; Path=/; SameSite=Strict", userCookie("laura-mendez"))
    }

    @Test
    fun `the page carries the token cookie and the user cookie`() {
        val page = deskPage("http://192.168.1.20:8000", "sa_tok", topic, "laura-mendez")!!

        assertEquals("sa_token=sa_tok; Path=/; HttpOnly; SameSite=Strict", page.cookie)
        assertEquals("sa_user=laura-mendez; Path=/; SameSite=Strict", page.userCookie)
        assertEquals("http://192.168.1.20:8000/", page.cookieUrl)
        assertFalse(page.toString().contains("laura-mendez") && page.toString().contains("sa_tok"))
    }

    @Test
    fun `without a user there is no user cookie`() {
        assertNull(deskPage("http://192.168.1.20:8000", "sa_tok", topic)!!.userCookie)
    }
}
