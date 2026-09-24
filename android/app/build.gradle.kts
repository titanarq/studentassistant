plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
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

    sourceSets {
        // The shared protocol fixtures (the contract test, docs/modules/protocol.md) reach the JVM
        // unit tests straight from the repository's own protocol/ directory -- never a copy under
        // android/. They land on the test classpath as `examples/<name>.json`.
        getByName("test") {
            resources.srcDir(rootProject.file("../protocol"))
        }
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

    // The v1 wire contract (`protocol` package): @Serializable classes and their JSON codec.
    implementation(libs.kotlinx.serialization.json)

    // View models (pairing, paired backends, connection test) and lifecycle-aware Compose state.
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)

    // The backend client (`backend` package) and the paired-backends store.
    implementation(libs.okhttp)
    implementation(libs.androidx.datastore.core)

    // The pairing QR scanner: CameraX preview + ImageAnalysis, decoded with ZXing core.
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.view)
    implementation(libs.zxing.core)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
}
