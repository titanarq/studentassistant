// Root build: declares the plugin versions once, from the catalog, and applies none of them.
// Each module applies what it needs with `alias(libs.plugins...)` and sets `jvmToolchain(17)`.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
