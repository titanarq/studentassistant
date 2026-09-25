package com.titanarq.studentassistant.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.titanarq.studentassistant.Clock
import com.titanarq.studentassistant.backend.BackendClient
import com.titanarq.studentassistant.backend.BackendCredentials
import com.titanarq.studentassistant.backend.BackendResult
import com.titanarq.studentassistant.backend.BackendStore
import com.titanarq.studentassistant.protocol.Session
import com.titanarq.studentassistant.protocol.SessionStartRequest
import com.titanarq.studentassistant.protocol.Subject
import com.titanarq.studentassistant.protocol.SubjectCreateRequest
import com.titanarq.studentassistant.protocol.Topic
import com.titanarq.studentassistant.protocol.TopicCreateRequest
import com.titanarq.studentassistant.session.OpenSession
import com.titanarq.studentassistant.session.SessionHolder
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** A list the home screen fetches from the backend. */
sealed interface Loadable<out T> {
    data object Loading : Loadable<Nothing>

    data class Loaded<T>(val value: T) : Loadable<T>

    data class Failed(val failure: BackendResult.Failure) : Loadable<Nothing>
}

/**
 * One topic row. [lastSessionAtMs] and [pendingCount] come from the topic list (protocol 1.1)
 * and are shown only when the backend reports them; a 1.0 backend sends neither.
 */
data class TopicRow(
    val topic: Topic,
    val lastSessionAtMs: Long? = null,
    val pendingCount: Int? = null,
) {
    /** True when the topic has an unended session: the row offers "Continuar", not "Empezar". */
    val canContinue: Boolean get() = topic.openSessionId != null
}

/** Why a session could not be started or continued. */
sealed interface SessionFailure {
    /** 409: another session is open (or the one to continue already ended); the list is refreshed. */
    data object Conflict : SessionFailure

    data class Backend(val failure: BackendResult.Failure) : SessionFailure
}

/** Starting or continuing a session from a topic row. */
sealed interface SessionAction {
    data object Idle : SessionAction

    data class Opening(val topicId: String) : SessionAction

    data class Failed(val topicId: String, val failure: SessionFailure) : SessionAction
}

/** The create-topic dialog while it is open. */
data class CreateTopicDialog(
    val saving: Boolean = false,
    val failure: BackendResult.Failure? = null,
)

data class HomeUiState(
    /** The active backend's display name; null until read or when none is stored. */
    val backendName: String? = null,
    /** True once the store was read and holds no active backend. */
    val noBackend: Boolean = false,
    val subjects: Loadable<List<Subject>> = Loadable.Loading,
    /** The subject whose topics are shown; null shows the subject list. */
    val selectedSubject: Subject? = null,
    /** The selected subject's topics; null while no subject is selected. */
    val topics: Loadable<List<TopicRow>>? = null,
    /** The create-topic dialog; null when it is closed. */
    val createTopic: CreateTopicDialog? = null,
    val session: SessionAction = SessionAction.Idle,
    /** Set once a session was opened: the screen navigates to capture and calls [HomeViewModel.onSessionShown]. */
    val openedSession: Session? = null,
)

/**
 * The home screen: the active backend's subjects and their topics, creating a topic (and its
 * subject when it is new), and starting ("Empezar sesión") or resuming ("Continuar") a session,
 * which is handed to the capture screen through [SessionHolder].
 */
class HomeViewModel(
    private val client: BackendClient,
    private val store: BackendStore,
    private val sessions: SessionHolder,
    private val clock: Clock,
) : ViewModel() {
    private val _state = MutableStateFlow(HomeUiState())
    val state: StateFlow<HomeUiState> = _state.asStateFlow()

    private var backend: BackendCredentials? = null
    private var loadJob: Job? = null
    private var topicsJob: Job? = null
    private var createJob: Job? = null
    private var sessionJob: Job? = null

    /**
     * (Re)loads the active backend's subjects and, when a subject is selected and still exists,
     * its topics. Called whenever the home screen is shown, so a switched backend or a session
     * opened or ended elsewhere is picked up.
     */
    fun load() {
        loadJob?.cancel()
        loadJob = viewModelScope.launch {
            val active = store.active()
            if (active == null) {
                backend = null
                _state.value = HomeUiState(noBackend = true)
                return@launch
            }
            val changed = backend != active.credentials
            backend = active.credentials
            _state.update {
                val keep = if (changed) HomeUiState() else it
                keep.copy(backendName = active.displayName, noBackend = false, subjects = Loadable.Loading)
            }
            val subjects = client.listSubjects(active.credentials).toLoadable { it.subjects }
            val selected = _state.value.selectedSubject?.let { current ->
                (subjects as? Loadable.Loaded)?.value?.firstOrNull { it.subjectId == current.subjectId }
                    ?: current.takeIf { subjects !is Loadable.Loaded }
            }
            _state.update { it.copy(subjects = subjects, selectedSubject = selected, topics = if (selected == null) null else it.topics) }
            if (selected != null) loadTopics(selected)
        }
    }

    /** Shows [subject]'s topics. */
    fun selectSubject(subject: Subject) {
        _state.update { it.copy(selectedSubject = subject, topics = Loadable.Loading, session = SessionAction.Idle) }
        loadTopics(subject)
    }

    /** Back from a subject's topics to the subject list. */
    fun clearSubject() {
        topicsJob?.cancel()
        _state.update { it.copy(selectedSubject = null, topics = null, session = SessionAction.Idle) }
    }

    /** Reloads the selected subject's topics. */
    fun refreshTopics() {
        _state.value.selectedSubject?.let { loadTopics(it) }
    }

    fun openCreateTopic() {
        _state.update { it.copy(createTopic = CreateTopicDialog()) }
    }

    fun dismissCreateTopic() {
        if (_state.value.createTopic?.saving == true) return
        _state.update { it.copy(createTopic = null) }
    }

    /**
     * Creates topic [title] in the subject named [subjectName]: an existing subject when the name
     * matches one (ignoring case and surrounding spaces), else a new subject created first. On
     * success the dialog closes and the subject's topics are shown.
     */
    fun createTopic(subjectName: String, title: String) {
        val name = subjectName.trim()
        val topicName = title.trim()
        val credentials = backend ?: return
        if (name.isEmpty() || topicName.isEmpty() || createJob?.isActive == true) return
        _state.update { it.copy(createTopic = CreateTopicDialog(saving = true)) }
        createJob = viewModelScope.launch {
            val known = (_state.value.subjects as? Loadable.Loaded)?.value.orEmpty()
            val existing = known.firstOrNull { it.name.trim().equals(name, ignoreCase = true) }
            val subject = existing ?: when (val created = client.createSubject(credentials, SubjectCreateRequest(name))) {
                is BackendResult.Success -> created.value
                is BackendResult.Failure -> return@launch failCreate(created)
            }
            if (existing == null) {
                _state.update { it.copy(subjects = Loadable.Loaded(known + subject)) }
            }
            when (val topic = client.createTopic(credentials, subject.subjectId, TopicCreateRequest(topicName))) {
                is BackendResult.Success -> {
                    _state.update { it.copy(createTopic = null) }
                    selectSubject(subject)
                }
                is BackendResult.Failure -> failCreate(topic)
            }
        }
    }

    /** "Empezar sesión" (no open session) or "Continuar" (resumes the topic's open session). */
    fun startOrContinue(row: TopicRow) {
        val credentials = backend ?: return
        val subject = _state.value.selectedSubject ?: return
        if (sessionJob?.isActive == true) return
        val topic = row.topic
        _state.update { it.copy(session = SessionAction.Opening(topic.topicId)) }
        sessionJob = viewModelScope.launch {
            val openId = topic.openSessionId
            val result = if (openId != null) {
                client.resumeSession(credentials, openId)
            } else {
                client.startSession(
                    credentials,
                    SessionStartRequest(topic.subjectId, topic.topicId, clock.nowMillis()),
                )
            }
            when (result) {
                is BackendResult.Success -> {
                    sessions.open(OpenSession(credentials, result.value, subject.name, topic.name))
                    _state.update { it.copy(session = SessionAction.Idle, openedSession = result.value) }
                }
                is BackendResult.Failure -> {
                    val stale = result is BackendResult.HttpError && (result.status == 409 || result.status == 404)
                    val failure = if (stale && result.status == 409) SessionFailure.Conflict else SessionFailure.Backend(result)
                    _state.update { it.copy(session = SessionAction.Failed(topic.topicId, failure)) }
                    // The list no longer matches the backend: show the session that is really open.
                    if (stale) loadTopics(subject, keepSession = true)
                }
            }
        }
    }

    /** Hides a start/continue failure. */
    fun dismissSessionFailure() {
        _state.update { it.copy(session = SessionAction.Idle) }
    }

    /** The screen navigated to the opened session. */
    fun onSessionShown() {
        _state.update { it.copy(openedSession = null) }
    }

    private fun failCreate(failure: BackendResult.Failure) {
        _state.update { it.copy(createTopic = CreateTopicDialog(saving = false, failure = failure)) }
    }

    private fun loadTopics(subject: Subject, keepSession: Boolean = false) {
        val credentials = backend ?: return
        topicsJob?.cancel()
        topicsJob = viewModelScope.launch {
            val topics = client.listTopics(credentials, subject.subjectId).toLoadable { response ->
                response.topics.map { TopicRow(it, it.lastSessionAtMs, it.pendingCount) }
            }
            _state.update {
                if (it.selectedSubject?.subjectId != subject.subjectId) {
                    it
                } else {
                    it.copy(topics = topics, session = if (keepSession) it.session else SessionAction.Idle)
                }
            }
        }
    }

    private fun <T, R> BackendResult<T>.toLoadable(map: (T) -> R): Loadable<R> = when (this) {
        is BackendResult.Success -> Loadable.Loaded(map(value))
        is BackendResult.Failure -> Loadable.Failed(this)
    }
}
