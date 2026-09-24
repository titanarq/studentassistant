package com.titanarq.studentassistant

import org.junit.Assert.assertTrue
import org.junit.Test

/** Proves the JVM unit test task runs at all. */
class SanityTest {
    @Test
    fun `the jvm unit test task runs`() {
        assertTrue(SystemClock.nowMillis() > 0)
    }
}
