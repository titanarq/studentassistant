package com.titanarq.studentassistant

import android.app.Application

/** Creates the one [AppContainer] of the process; activities read it from here. */
class StudentAssistantApp : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer()
    }
}
