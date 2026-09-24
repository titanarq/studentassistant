plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.titanarq.studentassistant"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.titanarq.studentassistant"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    // The BOM aligns every Compose artifact; the ones below carry no version of their own.
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.ui.tooling.preview)

    // `ComponentActivity` + `setContent` for the launcher activity.
    implementation(libs.androidx.activity.compose)

    testImplementation(libs.junit)
}
