package com.titanarq.studentassistant.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class VersionTest {
    @Test
    fun `the current version is 1_5`() {
        assertEquals("1.5", PROTOCOL_VERSION)
        assertEquals(ProtocolVersion(1, 5), parseVersion(PROTOCOL_VERSION))
    }

    @Test
    fun `malformed versions are rejected`() {
        for (bad in listOf("1", "1.0.0", "01.0", "v1.0", "", "1.x")) {
            assertThrows(bad, IllegalArgumentException::class.java) { parseVersion(bad) }
        }
    }

    @Test
    fun `same major is compatible and negotiates the lower minor`() {
        assertTrue(isCompatible("1.7"))
        checkCompatible("1.7")
        assertEquals("1.5", negotiate("1.7"))
        assertEquals("1.1", negotiate("1.1"))
        assertEquals("1.2", negotiate("1.2", ours = "1.10"))
    }

    @Test
    fun `another major is refused naming both versions`() {
        assertFalse(isCompatible("2.0"))
        val e = assertThrows(IncompatibleProtocolVersionException::class.java) { checkCompatible("2.0") }
        assertEquals(
            "incompatible protocol_version 2.0: this side speaks 1.5; " +
                "update the older side so both share MAJOR version 1",
            e.message,
        )
        assertThrows(IncompatibleProtocolVersionException::class.java) { negotiate("0.9") }
    }
}
