@file:OptIn(ExperimentalCoroutinesApi::class)

package com.titanarq.studentassistant.capture

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AudioStreamerTest {
    @Test
    fun `reads 100 ms frames that tile the client timeline`() = runTest {
        val clock = FakeClock(50_000)
        val source = FakeAudioSource(totalSamples = 1600 * 3 + 100)
        val streamer = AudioStreamer(source, clock, StandardTestDispatcher(testScheduler))
        val frames = mutableListOf<Pair<ShortArray, Long>>()
        var errors = 0
        streamer.start(this, onFrame = { samples, t -> frames += samples to t }, onError = { errors++ })
        advanceUntilIdle()

        assertEquals(1600, streamer.samplesPerFrame)
        assertEquals(3, frames.size)
        assertEquals(listOf(49_900L, 50_000L, 50_100L), frames.map { it.second })
        assertEquals((0 until 1600).map { it.toShort() }, frames[0].first.toList())
        assertEquals(1600.toShort(), frames[1].first[0])
        // The source ran dry mid-frame: reported, and closed.
        assertEquals(1, errors)
        assertTrue(source.closed)
    }

    @Test
    fun `a source that cannot open reports an error`() = runTest {
        val source = FakeAudioSource(totalSamples = 0, canOpen = false)
        val streamer = AudioStreamer(source, FakeClock(), StandardTestDispatcher(testScheduler))
        var errors = 0
        streamer.start(this, onFrame = { _, _ -> error("no frames expected") }, onError = { errors++ })
        advanceUntilIdle()
        assertEquals(1, errors)
    }
}
