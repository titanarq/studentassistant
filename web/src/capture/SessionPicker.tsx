/**
 * The capture page's first step (#40): choose the subject and the topic of the session, create
 * either when it is missing, and open the session -- resuming the topic's `open_session_id` when
 * it has one and starting a new one otherwise, since at most one session is unended per backend
 * (docs/modules/server.md), so a start on a topic that already has one is refused and its Spanish
 * `detail` is shown as it comes. Once the session is open the picker gives way to it: a session
 * belongs to exactly one topic and there is no topic switch inside it.
 *
 * Every answer arrives decoded by `capture/api.ts` through the protocol bindings, so a body of any
 * other shape is a Spanish error here, never data. Nothing is stored in the browser: the page runs
 * on the PC itself, which the backend trusts while it listens on loopback, so there is no token to
 * ask for or to keep.
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { Session, Subject, Topic } from "../protocol";
import {
  type ApiResult,
  createSubject,
  createTopic,
  listSubjects,
  listTopics,
  resumeSession,
  startSession,
} from "./api";
import { describeFailure } from "./failures";

/** The session the student opened, with the names the picker showed it under. */
export interface OpenedSession {
  session: Session;
  subjectName: string;
  topicName: string;
}

/** The topic the student wants to ask the tutor about (#82), with the names the picker showed. */
export interface TutorTopic {
  subjectId: string;
  topicId: string;
  subjectName: string;
  topicName: string;
}

export interface SessionPickerProps {
  /**
   * Called once with the session the student opened, so the capture screen can take over. Without
   * it the picker reports the open session itself and `/capture` is never a dead end.
   */
  onSession?: (opened: OpenedSession) => void;
  /** The client clock the start request's `client_time_ms` is read from. */
  now?: () => number;
  /** When given, the chosen topic also offers "Preguntar al tutor", which calls it (#82). */
  onTutor?: (topic: TutorTopic) => void;
}

type SubjectsState =
  | { state: "loading" }
  | { state: "ready"; subjects: Subject[] }
  | { state: "failed"; message: string };

type TopicsState =
  | { state: "loading" }
  | { state: "ready"; topics: Topic[] }
  | { state: "failed"; message: string };

type OpeningState = { state: "idle" } | { state: "opening" } | { state: "failed"; message: string };

/** The subject whose topics are shown, and what loading them answered. */
interface Selection {
  subjectId: string;
  topics: TopicsState;
}

const SUBJECTS_FAILURE = "No se han podido cargar las asignaturas";
const TOPICS_FAILURE = "No se han podido cargar los temas";
const START_FAILURE = "No se ha podido empezar la sesión";
const RESUME_FAILURE = "No se ha podido continuar la sesión";

/** What a topic's row says besides its name: an unended session and the doubts waiting on it. */
function topicHint(topic: Topic): string | null {
  const hints: string[] = [];
  if (topic.open_session_id !== undefined) hints.push("sesión sin terminar");
  const pending = topic.pending_count;
  if (pending !== undefined && pending > 0) {
    hints.push(pending === 1 ? "1 duda pendiente" : `${pending} dudas pendientes`);
  }
  return hints.length === 0 ? null : hints.join(", ");
}

interface CreateFormLabels {
  form: string;
  field: string;
  submit: string;
  saving: string;
  empty: string;
  failure: string;
}

const SUBJECT_FORM: CreateFormLabels = {
  form: "Crear una asignatura",
  field: "Nombre de la asignatura",
  submit: "Crear la asignatura",
  saving: "Creando la asignatura…",
  empty: "Escribe el nombre de la asignatura.",
  failure: "No se ha podido crear la asignatura",
};

const TOPIC_FORM: CreateFormLabels = {
  form: "Crear un tema",
  field: "Nombre del tema",
  submit: "Crear el tema",
  saving: "Creando el tema…",
  empty: "Escribe el nombre del tema.",
  failure: "No se ha podido crear el tema",
};

/**
 * The "create a subject" / "create a topic" form: one name, posted as the bindings' own request
 * body, and the created item handed back so the list grows without a second round trip and the new
 * item becomes the chosen one.
 */
function CreateForm<T>({
  labels,
  create,
  onCreated,
}: {
  labels: CreateFormLabels;
  create: (name: string) => Promise<ApiResult<T>>;
  onCreated: (created: T) => void;
}) {
  const [name, setName] = useState("");
  const [status, setStatus] = useState<
    { state: "idle" } | { state: "saving" } | { state: "failed"; message: string }
  >({ state: "idle" });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed === "") {
      setStatus({ state: "failed", message: labels.empty });
      return;
    }
    setStatus({ state: "saving" });
    const result = await create(trimmed);
    if (result.kind === "ok") {
      setName("");
      setStatus({ state: "idle" });
      onCreated(result.value);
      return;
    }
    setStatus({ state: "failed", message: describeFailure(labels.failure, result) });
  }

  const saving = status.state === "saving";
  return (
    <form aria-label={labels.form} onSubmit={submit}>
      <h3>{labels.form}</h3>
      <p>
        <label>
          {labels.field}{" "}
          <input
            type="text"
            value={name}
            disabled={saving}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
      </p>
      <p>
        <button type="submit" disabled={saving}>
          {saving ? labels.saving : labels.submit}
        </button>
      </p>
      {status.state === "failed" && <p role="alert">{status.message}</p>}
    </form>
  );
}

export default function SessionPicker({ onSession, now = Date.now, onTutor }: SessionPickerProps) {
  const [subjects, setSubjects] = useState<SubjectsState>({ state: "loading" });
  const [subjectsAttempt, setSubjectsAttempt] = useState(0);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [topicsAttempt, setTopicsAttempt] = useState(0);
  const [topicId, setTopicId] = useState<string | null>(null);
  const [opening, setOpening] = useState<OpeningState>({ state: "idle" });
  const [opened, setOpened] = useState<OpenedSession | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSubjects({ state: "loading" });
    listSubjects().then((result) => {
      if (cancelled) return;
      setSubjects(
        result.kind === "ok"
          ? { state: "ready", subjects: result.value.subjects }
          : { state: "failed", message: describeFailure(SUBJECTS_FAILURE, result) },
      );
    });
    return () => {
      cancelled = true;
    };
  }, [subjectsAttempt]);

  const subjectId = selection?.subjectId ?? null;

  useEffect(() => {
    if (subjectId === null) return;
    const wanted = subjectId;
    let cancelled = false;
    listTopics(wanted).then((result) => {
      if (cancelled) return;
      setSelection((current) =>
        // Another subject may have been chosen while these topics were on their way.
        current === null || current.subjectId !== wanted
          ? current
          : {
              ...current,
              topics:
                result.kind === "ok"
                  ? { state: "ready", topics: result.value.topics }
                  : { state: "failed", message: describeFailure(TOPICS_FAILURE, result) },
            },
      );
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicsAttempt]);

  const chooseSubject = useCallback((chosen: string) => {
    setSelection({ subjectId: chosen, topics: { state: "loading" } });
    setTopicId(null);
    setOpening({ state: "idle" });
  }, []);

  const chooseTopic = useCallback((chosen: string) => {
    setTopicId(chosen);
    setOpening({ state: "idle" });
  }, []);

  const retrySubjects = useCallback(() => setSubjectsAttempt((attempt) => attempt + 1), []);

  const retryTopics = useCallback(() => {
    setSelection((current) =>
      current === null ? current : { ...current, topics: { state: "loading" } },
    );
    setTopicsAttempt((attempt) => attempt + 1);
  }, []);

  const addSubject = useCallback(
    (subject: Subject) => {
      // The backend's own list is the authority: it is re-read rather than appended to.
      setSubjects({ state: "loading" });
      setSubjectsAttempt((attempt) => attempt + 1);
      chooseSubject(subject.subject_id);
    },
    [chooseSubject],
  );

  const addTopic = useCallback(
    (topic: Topic) => {
      setSelection((current) =>
        current === null || current.subjectId !== topic.subject_id
          ? current
          : { ...current, topics: { state: "loading" } },
      );
      setTopicsAttempt((attempt) => attempt + 1);
      chooseTopic(topic.topic_id);
    },
    [chooseTopic],
  );

  const subjectName =
    subjects.state === "ready"
      ? subjects.subjects.find((subject) => subject.subject_id === subjectId)?.name
      : undefined;
  const chosenTopic =
    selection?.topics.state === "ready"
      ? selection.topics.topics.find((topic) => topic.topic_id === topicId)
      : undefined;

  async function openSession(topic: Topic) {
    const unended = topic.open_session_id;
    setOpening({ state: "opening" });
    const result =
      unended === undefined
        ? await startSession(topic.subject_id, topic.topic_id, now())
        : await resumeSession(unended);
    if (result.kind === "ok") {
      const session: OpenedSession = {
        session: result.value,
        subjectName: subjectName ?? result.value.subject_id,
        topicName: topic.name,
      };
      setOpening({ state: "idle" });
      setOpened(session);
      onSession?.(session);
      return;
    }
    setOpening({
      state: "failed",
      message: describeFailure(unended === undefined ? START_FAILURE : RESUME_FAILURE, result),
    });
  }

  if (opened !== null) {
    return (
      <main>
        <h1>Capturar una sesión de estudio</h1>
        <p role="status">
          Sesión {opened.session.session_id} en marcha: {opened.subjectName}, {opened.topicName}.
        </p>
      </main>
    );
  }

  return (
    <main>
      <h1>Capturar una sesión de estudio</h1>
      <p>
        Elige la asignatura y el tema. Una sesión es siempre de un solo tema: para cambiar de tema
        hay que terminarla y empezar otra.
      </p>

      <section aria-label="Asignaturas">
        <h2>Asignaturas</h2>
        {subjects.state === "loading" && <p>Cargando las asignaturas…</p>}
        {subjects.state === "failed" && (
          <>
            <p role="alert">{subjects.message}</p>
            <button type="button" onClick={retrySubjects}>
              Reintentar
            </button>
          </>
        )}
        {subjects.state === "ready" && (
          <>
            {subjects.subjects.length === 0 && (
              <p>Todavía no hay ninguna asignatura: crea la primera.</p>
            )}
            {subjects.subjects.length > 0 && (
              <ul>
                {subjects.subjects.map((subject) => (
                  <li key={subject.subject_id}>
                    <button
                      type="button"
                      aria-current={subject.subject_id === subjectId ? "true" : undefined}
                      onClick={() => chooseSubject(subject.subject_id)}
                    >
                      {subject.name}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
        <CreateForm labels={SUBJECT_FORM} create={createSubject} onCreated={addSubject} />
      </section>

      {selection !== null && (
        <section aria-label="Temas">
          <h2>{subjectName === undefined ? "Temas" : `Temas de ${subjectName}`}</h2>
          {selection.topics.state === "loading" && <p>Cargando los temas…</p>}
          {selection.topics.state === "failed" && (
            <>
              <p role="alert">{selection.topics.message}</p>
              <button type="button" onClick={retryTopics}>
                Reintentar
              </button>
            </>
          )}
          {selection.topics.state === "ready" && (
            <>
              {selection.topics.topics.length === 0 && (
                <p>Esta asignatura todavía no tiene ningún tema: crea el primero.</p>
              )}
              {selection.topics.topics.length > 0 && (
                <ul>
                  {selection.topics.topics.map((topic) => {
                    const hint = topicHint(topic);
                    return (
                      <li key={topic.topic_id}>
                        <button
                          type="button"
                          aria-current={topic.topic_id === topicId ? "true" : undefined}
                          onClick={() => chooseTopic(topic.topic_id)}
                        >
                          {topic.name}
                        </button>
                        {hint !== null && <span> ({hint})</span>}
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
          <CreateForm
            labels={TOPIC_FORM}
            create={(name) => createTopic(selection.subjectId, name)}
            onCreated={addTopic}
          />
        </section>
      )}

      {chosenTopic !== undefined && (
        <section aria-label="Sesión">
          <h2>La sesión</h2>
          <p>
            {subjectName === undefined ? chosenTopic.subject_id : subjectName} ·{" "}
            {chosenTopic.name}
          </p>
          <p>
            {chosenTopic.open_session_id === undefined
              ? "Este tema no tiene ninguna sesión sin terminar."
              : "Este tema tiene una sesión sin terminar: puedes continuarla donde la dejaste."}
          </p>
          <button
            type="button"
            disabled={opening.state === "opening"}
            onClick={() => void openSession(chosenTopic)}
          >
            {opening.state === "opening"
              ? "Abriendo la sesión…"
              : chosenTopic.open_session_id === undefined
                ? "Empezar una sesión nueva"
                : "Continuar la sesión abierta"}
          </button>
          {opening.state === "failed" && <p role="alert">{opening.message}</p>}
        </section>
      )}

      {chosenTopic !== undefined && onTutor !== undefined && (
        <section aria-label="Tutor">
          <h2>Estudiar con el tutor</h2>
          <p>Pregúntale por voz sobre este tema: contesta con tus apuntes y tus fuentes.</p>
          <button
            type="button"
            onClick={() =>
              onTutor({
                subjectId: chosenTopic.subject_id,
                topicId: chosenTopic.topic_id,
                subjectName: subjectName ?? chosenTopic.subject_id,
                topicName: chosenTopic.name,
              })
            }
          >
            Preguntar al tutor
          </button>
        </section>
      )}
    </main>
  );
}
