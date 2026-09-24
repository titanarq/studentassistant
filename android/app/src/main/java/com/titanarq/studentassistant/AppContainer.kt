package com.titanarq.studentassistant

/** Wall-clock time source, so time-dependent logic can be tested with a fake. */
fun interface Clock {
    fun nowMillis(): Long
}

/** The real [Clock], backed by [System.currentTimeMillis]. */
object SystemClock : Clock {
    override fun nowMillis(): Long = System.currentTimeMillis()
}

/**
 * Manual constructor DI (AGENTS.md): the app-wide object graph, created once by
 * [StudentAssistantApp]. Plain Kotlin with no Android types so it is testable on the JVM.
 * Later tasks add their singletons here (backend client, pairing store, spool, ...) as
 * `by lazy` members, and pass constructor overrides for fakes in tests.
 */
class AppContainer(
    clockFactory: () -> Clock = { SystemClock },
) {
    /** The app-wide clock, created on first access and shared afterwards. */
    val clock: Clock by lazy(clockFactory)
}
