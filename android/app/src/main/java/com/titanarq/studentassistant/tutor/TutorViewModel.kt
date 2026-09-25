package com.titanarq.studentassistant.tutor

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendStore
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicInteger

/** The longest question the backend takes (`editor.tutor.MAX_QUESTION_CHARS`). */
const val MAX_QUESTION_CHARS = 1000

/** Which backend the screen talks to. */
sealed interface TutorSetup {
    data object Loading : TutorSetup

    /** No active backend is stored. */
    data object NoBackend : TutorSetup

    data class Ready(val backendName: String) : TutorSetup
}

/** The topic's earlier questions. */
sealed interface TutorHistoryState {
    data object Loading : TutorHistoryState

    data object Loaded : TutorHistoryState

    data class Failed(val failure: TutorResult.Failure) : TutorHistoryState
}

/** The question being answered and the answer streamed so far. */
data class PendingQuestion(val question: String, val partial: String = "")

/** A question that got no answer, and why. */
data class AskFailure(val question: String, val failure: TutorResult.Failure) {
    /** A reached cost cap: «Continuar igualmente» asks it again past the cap. */
    val overCap: Boolean get() = (failure as? TutorResult.Refused)?.overCap == true
}

/** What the tutor screen shows. */
data class TutorUiState(
    val setup: TutorSetup = TutorSetup.Loading,
    val history: TutorHistoryState = TutorHistoryState.Loading,
    /** The topic's questions and answers, oldest first, the ones asked here included. */
    val turns: List<TutorTurn> = emptyList(),
    /** The typed question. */
    val draft: String = "",
    /** A spoken question is being listened to; [heard] is its text so far. */
    val listening: Boolean = false,
    val heard: String = "",
    val voiceProblem: VoiceProblem? = null,
    val pending: PendingQuestion? = null,
    val failure: AskFailure? = null,
    /** Answers are read aloud when they arrive («Leer las respuestas en voz alta»). */
    val readAloud: Boolean = true,
    val speaking: Boolean = false,
    /** False once the phone turned out to have no Spanish synthesis. */
    val speechAvailable: Boolean = true,
) {
    /** A question can be asked now: a backend is known and no other question is being answered. */
    val canAsk: Boolean get() = setup is TutorSetup.Ready && pending == null
}

/**
 * The voice tutor screen (#248) for [topic] on the active backend: the topic's earlier questions
 * (`GET .../tutor`), a question spoken ([VoiceQuestion], one utterance) or typed, its answer
 * streamed (`POST .../tutor`) and read aloud with [speech] (the app's one synthesizer) without its
 * footnote marks. A reached
 * cost cap can be passed with [confirmOverCap]. No tutoring logic here: the backend answers
 * (ADR-0001).
 *
 * Every call comes from the main thread; stream progress and the end of speech may arrive on
 * others, and only update [state].
 */
class TutorViewModel(
    private val client: TutorClient,
    private val store: BackendStore,
    val topic: TutorTopic,
    private val voice: VoiceQuestion,
    private val speech: SpeechOutput,
) : ViewModel() {
    private val _state = MutableStateFlow(TutorUiState(speechAvailable = speech.available))
    val state: StateFlow<TutorUiState> = _state.asStateFlow()

    private var backend: BackendCredentials? = null
    private val speechIds = AtomicInteger()

    init {
        viewModelScope.launch {
            val active = store.active()
            if (active == null) {
                _state.update { it.copy(setup = TutorSetup.NoBackend, history = TutorHistoryState.Loaded) }
            } else {
                backend = active.credentials
                _state.update { it.copy(setup = TutorSetup.Ready(active.displayName)) }
                loadHistory()
            }
        }
    }

    /** Loads the topic's earlier questions again («Reintentar»). */
    fun retryHistory() {
        if (backend == null) return
        viewModelScope.launch { loadHistory() }
    }

    fun onDraftChange(text: String) {
        _state.update { it.copy(draft = text.take(MAX_QUESTION_CHARS)) }
    }

    /** Asks the typed question («Preguntar»). */
    fun askDraft() {
        val question = _state.value.draft.trim()
        if (question.isEmpty() || !_state.value.canAsk) return
        _state.update { it.copy(draft = "") }
        ask(question, confirmOverCap = false)
    }

    /** «Preguntar por voz»: listens for one question and asks it as soon as it is recognised. */
    fun startVoice() {
        val current = _state.value
        if (!current.canAsk || current.listening) return
        stopSpeaking()
        _state.update { it.copy(listening = true, heard = "", voiceProblem = null, failure = null) }
        voice.start(
            object : VoiceQuestion.Listener {
                override fun onInterim(text: String) {
                    _state.update { it.copy(heard = text) }
                }

                override fun onFinal(text: String) {
                    _state.update { it.copy(listening = false, heard = "") }
                    ask(text.take(MAX_QUESTION_CHARS), confirmOverCap = false)
                }

                override fun onProblem(problem: VoiceProblem) {
                    _state.update { it.copy(listening = false, heard = "", voiceProblem = problem) }
                }
            },
        )
    }

    /** «Ya he terminado»: ends the spoken question now, asking what was heard. */
    fun stopVoice() {
        voice.stop()
    }

    /** The student refused the microphone permission. */
    fun onMicPermissionDenied() {
        _state.update { it.copy(voiceProblem = VoiceProblem.PERMISSION_DENIED) }
    }

    fun dismissVoiceProblem() {
        _state.update { it.copy(voiceProblem = null) }
    }

    /** «Continuar igualmente»: asks the question the cost cap stopped again, past the cap. */
    fun confirmOverCap() {
        val failure = _state.value.failure?.takeIf { it.overCap } ?: return
        if (!_state.value.canAsk) return
        ask(failure.question, confirmOverCap = true)
    }

    /** «Reintentar»: asks the question that got no answer again. */
    fun retryFailed() {
        val failure = _state.value.failure ?: return
        if (!_state.value.canAsk) return
        ask(failure.question, confirmOverCap = false)
    }

    fun dismissFailure() {
        _state.update { it.copy(failure = null) }
    }

    /** «Leer las respuestas en voz alta»: off stops the reading in progress. */
    fun setReadAloud(enabled: Boolean) {
        _state.update { it.copy(readAloud = enabled) }
        if (!enabled) stopSpeaking()
    }

    /** «Leer otra vez»: reads [turn]'s answer aloud. */
    fun readAgain(turn: TutorTurn) {
        read(turn.reply)
    }

    /** «Parar de leer». */
    fun stopSpeaking() {
        speechIds.incrementAndGet()
        speech.stop()
        _state.update { it.copy(speaking = false) }
    }

    /** The screen went to the background: no listening or reading while it is not seen. */
    fun onBackground() {
        // Also frees the platform recognizer; the next spoken question creates a new one.
        voice.release()
        _state.update { it.copy(listening = false, heard = "") }
        stopSpeaking()
    }

    override fun onCleared() {
        voice.release()
        // [speech] is the app's one synthesizer: stopped, not shut down.
        speechIds.incrementAndGet()
        speech.stop()
    }

    private suspend fun loadHistory() {
        val credentials = backend ?: return
        _state.update { it.copy(history = TutorHistoryState.Loading) }
        when (val result = client.history(credentials, topic.subjectId, topic.topicId)) {
            is TutorResult.Success -> _state.update {
                // An answer that arrived meanwhile is in the history too (the backend records it first).
                it.copy(history = TutorHistoryState.Loaded, turns = result.value)
            }
            is TutorResult.Failure -> _state.update { it.copy(history = TutorHistoryState.Failed(result)) }
        }
    }

    private fun ask(question: String, confirmOverCap: Boolean) {
        val credentials = backend ?: return
        if (question.isBlank() || !_state.value.canAsk) return
        stopSpeaking()
        _state.update { it.copy(pending = PendingQuestion(question), failure = null, voiceProblem = null) }
        viewModelScope.launch {
            val result = client.ask(credentials, topic.subjectId, topic.topicId, question, confirmOverCap) { progress ->
                _state.update { state ->
                    val pending = state.pending ?: return@update state
                    val partial = when (progress) {
                        is TutorProgress.Delta -> pending.partial + progress.text
                        TutorProgress.Restart -> ""
                    }
                    state.copy(pending = pending.copy(partial = partial))
                }
            }
            when (result) {
                is TutorResult.Success -> {
                    val answer = result.value
                    val turn = TutorTurn(
                        question = answer.question.ifBlank { question },
                        reply = answer.reply,
                        refs = answer.refs,
                        warning = answer.warning,
                    )
                    _state.update { it.copy(pending = null, turns = it.turns + turn) }
                    if (_state.value.readAloud) read(answer.reply)
                }
                is TutorResult.Failure -> _state.update {
                    it.copy(pending = null, failure = AskFailure(question, result))
                }
            }
        }
    }

    private fun read(reply: String) {
        val text = spokenText(reply)
        if (text.isEmpty()) return
        val id = speechIds.incrementAndGet()
        _state.update { it.copy(speaking = true) }
        speech.speak(text) {
            if (speechIds.get() == id) _state.update { it.copy(speaking = false) }
        }
        _state.update { it.copy(speechAvailable = speech.available) }
    }
}
