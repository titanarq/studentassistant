package com.titanarq.studentassistant

import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test

class AppContainerTest {
    @Test
    fun `a container can be constructed with its defaults`() {
        val container = AppContainer()

        assertSame(SystemClock, container.clock)
    }

    @Test
    fun `a member is created lazily once and the same instance is returned on repeated access`() {
        var created = 0
        val container = AppContainer(clockFactory = {
            created++
            Clock { 42L }
        })
        assertEquals(0, created)

        val first = container.clock
        val second = container.clock

        assertSame(first, second)
        assertEquals(1, created)
        assertEquals(42L, first.nowMillis())
    }
}
