package com.titanarq.studentassistant.capture

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.titanarq.studentassistant.Clock
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** A blocking source of PCM16 mono samples at [sampleRateHz] (the microphone, or a fake). */
interface AudioSource {
    val sampleRateHz: Int

    /** Opens the source; false when it cannot (no permission, mic in use). */
    fun open(): Boolean

    /** Blocks until up to [length] samples are read into [buffer] at [offset]; the count, or a negative error. */
    fun read(buffer: ShortArray, offset: Int, length: Int): Int

    fun close()
}

/**
 * Server STT mode (ADR-0008): reads [source] on [dispatcher] and hands [onFrame] one frame of
 * [frameMs] ms (1600 samples at 16 kHz) at a time with the client time of its first sample. The
 * time is the wall clock at the first read plus the samples read since, so frames tile the
 * timeline without gaps. [onError] runs (on [dispatcher]) when the source cannot open or fails.
 */
class AudioStreamer(
    private val source: AudioSource,
    private val clock: Clock,
    private val dispatcher: CoroutineDispatcher,
    private val frameMs: Int = FRAME_MS,
) {
    private var job: Job? = null

    val samplesPerFrame: Int get() = source.sampleRateHz * frameMs / 1000

    fun start(scope: CoroutineScope, onFrame: (ShortArray, Long) -> Unit, onError: () -> Unit) {
        if (job != null) return
        job = scope.launch(dispatcher) {
            if (!source.open()) {
                onError()
                return@launch
            }
            try {
                var startMs = -1L
                var samplesSoFar = 0L
                while (isActive) {
                    val frame = ShortArray(samplesPerFrame)
                    var filled = 0
                    while (filled < frame.size && isActive) {
                        val read = source.read(frame, filled, frame.size - filled)
                        if (read < 0) {
                            onError()
                            return@launch
                        }
                        filled += read
                    }
                    if (filled < frame.size) break
                    if (startMs < 0) startMs = clock.nowMillis() - frameMs
                    val clientTimeMs = startMs + samplesSoFar * 1000 / source.sampleRateHz
                    samplesSoFar += frame.size
                    onFrame(frame, clientTimeMs)
                }
            } finally {
                source.close()
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
    }

    companion object {
        const val FRAME_MS: Int = 100
    }
}

/**
 * The microphone as an [AudioSource]: [AudioRecord] at 16 kHz mono PCM16 from the
 * voice-recognition input. The caller holds `RECORD_AUDIO` before [open].
 */
class AudioRecordSource(override val sampleRateHz: Int = 16_000) : AudioSource {
    private var record: AudioRecord? = null

    @SuppressLint("MissingPermission") // the capture screen asks for RECORD_AUDIO first
    override fun open(): Boolean {
        val minBuffer = AudioRecord.getMinBufferSize(
            sampleRateHz,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minBuffer <= 0) return false
        return try {
            val created = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                sampleRateHz,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuffer, sampleRateHz / 2 * 2), // at least 0.5 s of samples
            )
            if (created.state != AudioRecord.STATE_INITIALIZED) {
                created.release()
                return false
            }
            created.startRecording()
            record = created
            true
        } catch (e: SecurityException) {
            false
        } catch (e: IllegalStateException) {
            false
        }
    }

    override fun read(buffer: ShortArray, offset: Int, length: Int): Int =
        record?.read(buffer, offset, length) ?: -1

    override fun close() {
        record?.let {
            runCatching { it.stop() }
            it.release()
        }
        record = null
    }
}
