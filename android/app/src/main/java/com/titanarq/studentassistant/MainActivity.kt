package com.titanarq.studentassistant

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.titanarq.studentassistant.ui.PlaceholderScreen
import com.titanarq.studentassistant.ui.StudentAssistantTheme

class MainActivity : ComponentActivity() {
    /** The single app-wide container; screens will get their dependencies from it. */
    private val container: AppContainer by lazy { (application as StudentAssistantApp).container }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        container // resolve it now so a misregistered Application fails at startup
        setContent {
            StudentAssistantTheme {
                PlaceholderScreen()
            }
        }
    }
}
