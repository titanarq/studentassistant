package com.titanarq.studentassistant.share

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.home.Loadable
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.Topic
import com.titanarq.studentassistant.protocol.WebPageAddRequest
import com.titanarq.studentassistant.protocol.WebPageVia
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** What became of the shared page. */
sealed interface ShareOutcome {
    /** Stored in [topic] as [title]; [alreadyKept] when the topic had that page already. */
    data class Saved(val topic: Topic, val title: String, val alreadyKept: Boolean) : ShareOutcome

    /** The backend refused or could not be reached; the student may pick a topic again. */
    data class Failed(val topic: Topic, val failure: BackendResult.Failure) : ShareOutcome
}

data class ShareUiState(
    /** The link found in the shared text; null when there is none (nothing can be saved). */
    val url: String? = null,
    /** The active backend's display name; null until read or when none is stored. */
    val backendName: String? = null,
    /** True once the store was read and holds no active backend. */
    val noBackend: Boolean = false,
    val subjects: Loadable<List<Subject>> = Loadable.Loading,
    /** The subject whose topics are shown; null shows the subject list. */
    val selectedSubject: Subject? = null,
    /** The selected subject's topics, the one with an open session first; null with no subject. */
    val topics: Loadable<List<Topic>>? = null,
    /** The topic the page is being saved to, while the backend fetches it. */
    val saving: Topic? = null,
    val outcome: ShareOutcome? = null,
)

/**
 * "Compartir -> Student Assistant" (#62): the link another app shared is saved as a web source of
 * a topic the student picks among the active backend's subjects and topics. The backend fetches
 * and stores the page (`POST .../web-pages`, `via: share`); the phone only carries the address
 * (ADR-0001, thin client).
 */
class ShareViewModel(
    sharedText: String?,
    sharedSubject: String?,
    private val client: BackendClient,
    private val store: BackendStore,
) : ViewModel() {
    private val _state = MutableStateFlow(ShareUiState(url = extractSharedUrl(sharedText, sharedSubject)))
    val state: StateFlow<ShareUiState> = _state.asStateFlow()

    private var backend: BackendCredentials? = null
    private var topicsJob: Job? = null
    private var saveJob: Job? = null

    /** Reads the active backend and lists its subjects (nothing is called without a link). */
    fun load() {
        if (_state.value.url == null) return
        viewModelScope.launch {
            val active = store.active()
            if (active == null) {
                _state.update { it.copy(noBackend = true) }
                return@launch
            }
            backend = active.credentials
            _state.update { it.copy(backendName = active.displayName, noBackend = false, subjects = Loadable.Loading) }
            val subjects = when (val result = client.listSubjects(active.credentials)) {
                is BackendResult.Success -> Loadable.Loaded(result.value.subjects)
                is BackendResult.Failure -> Loadable.Failed(result)
            }
            _state.update { it.copy(subjects = subjects) }
        }
    }

    fun selectSubject(subject: Subject) {
        val credentials = backend ?: return
        _state.update { it.copy(selectedSubject = subject, topics = Loadable.Loading, outcome = null) }
        topicsJob?.cancel()
        topicsJob = viewModelScope.launch {
            val topics = when (val result = client.listTopics(credentials, subject.subjectId)) {
                // The topic being studied right now is the likely target: it goes first.
                is BackendResult.Success -> Loadable.Loaded(result.value.topics.sortedBy { it.openSessionId == null })
                is BackendResult.Failure -> Loadable.Failed(result)
            }
            _state.update { if (it.selectedSubject?.subjectId == subject.subjectId) it.copy(topics = topics) else it }
        }
    }

    /** Back from a subject's topics to the subject list. */
    fun clearSubject() {
        if (_state.value.saving != null) return
        topicsJob?.cancel()
        _state.update { it.copy(selectedSubject = null, topics = null, outcome = null) }
    }

    /** Saves the shared page to [topic]. */
    fun save(topic: Topic) {
        val credentials = backend ?: return
        val url = _state.value.url ?: return
        if (saveJob?.isActive == true || _state.value.outcome is ShareOutcome.Saved) return
        _state.update { it.copy(saving = topic, outcome = null) }
        saveJob = viewModelScope.launch {
            val request = WebPageAddRequest(url = url, via = WebPageVia.SHARE)
            val outcome = when (val result = client.addWebPage(credentials, topic.subjectId, topic.topicId, request)) {
                is BackendResult.Success -> ShareOutcome.Saved(topic, result.value.title, result.value.alreadyKept)
                is BackendResult.Failure -> ShareOutcome.Failed(topic, result)
            }
            _state.update { it.copy(saving = null, outcome = outcome) }
        }
    }
}
