package com.titanarq.studentassistant.tutor

import com.titanarq.studentassistant.backend.BackendCredentials
import kotlinx.coroutines.CompletableDeferred

/** A scripted [TutorClient]: [historyResult] answers every history call; each ask waits for [answer]. */
class FakeTutorClient : TutorClient {
    var historyResult: TutorResult<List<TutorTurn>> = TutorResult.Success(emptyList())
    val historyCalls = mutableListOf<BackendCredentials>()

    /** One recorded question: complete [reply] to end it; [progress] reports stream progress meanwhile. */
    class Ask(
        val backend: BackendCredentials,
        val subjectId: String,
        val topicId: String,
        val question: String,
        val confirmOverCap: Boolean,
        val progress: (TutorProgress) -> Unit,
    ) {
        val reply = CompletableDeferred<TutorResult<TutorAnswer>>()
    }

    val asks = mutableListOf<Ask>()

    override suspend fun history(backend: BackendCredentials, subjectId: String, topicId: String): TutorResult<List<TutorTurn>> {
        historyCalls += backend
        return historyResult
    }

    override suspend fun ask(
        backend: BackendCredentials,
        subjectId: String,
        topicId: String,
        question: String,
        confirmOverCap: Boolean,
        onProgress: (TutorProgress) -> Unit,
    ): TutorResult<TutorAnswer> {
        val ask = Ask(backend, subjectId, topicId, question, confirmOverCap, onProgress)
        asks += ask
        return ask.reply.await()
    }
}

/** A [SpeechOutput] that records what it reads; [finish] ends the current reading. */
class FakeSpeechOutput(override var available: Boolean = true) : SpeechOutput {
    val spoken = mutableListOf<String>()
    var stops = 0
        private set
    private var onDone: (() -> Unit)? = null

    override fun speak(text: String, onDone: () -> Unit) {
        this.onDone?.invoke()
        if (!available) {
            onDone()
            return
        }
        spoken += text
        this.onDone = onDone
    }

    override fun stop() {
        stops++
        onDone?.invoke()
        onDone = null
    }

    override fun shutdown() = stop()

    fun finish() {
        val done = onDone
        onDone = null
        done?.invoke()
    }
}
