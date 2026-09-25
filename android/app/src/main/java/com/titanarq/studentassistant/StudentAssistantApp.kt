package com.titanarq.studentassistant

import android.app.Application
import android.os.Build
import com.titanarq.studentassistant.capture.AndroidSpeechRecognizerEngine
import com.titanarq.studentassistant.capture.AudioRecordSource
import com.titanarq.studentassistant.capture.SpeechRecognizerTranscriber

/** Creates the one [AppContainer] of the process; activities read it from here. */
class StudentAssistantApp : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(
            filesDir = filesDir,
            deviceName = Build.MODEL ?: "Android",
            clientTranscriberFactory = { scope ->
                SpeechRecognizerTranscriber(AndroidSpeechRecognizerEngine(applicationContext), container.clock, scope)
            },
            audioSourceFactory = { AudioRecordSource() },
        )
    }
}
