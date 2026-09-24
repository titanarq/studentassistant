package com.titanarq.studentassistant.protocol

import java.io.File

/**
 * The repository's own `protocol/examples/`, put on the test classpath as `examples/` by the
 * test resources source directory in `app/build.gradle.kts` (never a copy under `android/`).
 */
object SharedExamples {
    private val dir: File by lazy {
        val url = checkNotNull(javaClass.classLoader?.getResource("examples/client.hello.json")) {
            "protocol/examples is not on the test classpath; see sourceSets.test in app/build.gradle.kts"
        }
        check(url.protocol == "file") { "expected the shared examples as files, got $url" }
        checkNotNull(File(url.toURI()).parentFile)
    }

    /** Every example, by message name (`client.hello` for `client.hello.json`). */
    fun all(): Map<String, String> =
        dir.listFiles { f -> f.isFile && f.name.endsWith(".json") }.orEmpty()
            .sortedBy { it.name }
            .associate { it.name.removeSuffix(".json") to it.readText() }

    fun read(name: String): String = File(dir, "$name.json").readText()
}
