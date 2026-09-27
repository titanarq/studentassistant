import { useEffect, useState } from "react";
import {
  describeFailure,
  fetchSubjects,
  fetchTopics,
  formatDate,
  type ReadResult,
} from "./desk/api";
import type { ActiveSession } from "./desk/costApi";
import DeskCost from "./desk/DeskCost";
import { NewSubject, NewTopic } from "./desk/DeskCreate";
import { topicCardPath, topicEntryPath, topicStudyPath, topicWorkspacePath } from "./desk/entry";
import DeskPractice from "./desk/DeskPractice";
import type { Subject, Topic } from "./protocol";
import { styleGuidePagePath } from "./styleGuide/api";
import "./desk/desk.css";

/**
 * `/`: the study desk (docs/VISION.md §2), the way into a topic's Construir and Estudiar (#368).
 * Every subject with its topics; each topic's name opens it where `topicEntryPath` says (the
 * workspace, Construir, by default), followed by what the topic list carries (an open session,
 * linking to the workspace, the last session's date and the doubts waiting for review) and two
 * small links, **Estudiar** (the study screen) and **Ficha** (the topic card page). "Nueva
 * asignatura" and each subject's "Nuevo tema" create them; a created topic opens in its
 * workspace. "Gasto de hoy" (`DeskCost`, #260) shows today's spend against the caps, and the open
 * session's when there is one; "Repasos para hoy" (`DeskPractice`, #285) lists the topics with
 * practice due or new today.
 */

type Desk =
  | { state: "loading" }
  | { state: "failed"; message: string }
  | { state: "ok"; subjects: { subject: Subject; topics: ReadResult<Topic[]> }[] };

function pendingText(count: number): string {
  return count === 1 ? "1 duda por revisar" : `${count} dudas por revisar`;
}

function openSession(desk: Desk): ActiveSession | undefined {
  if (desk.state !== "ok") return undefined;
  for (const { topics } of desk.subjects) {
    const open = topics.kind === "ok" ? topics.value.find((t) => t.open_session_id !== undefined) : undefined;
    if (open?.open_session_id !== undefined) {
      return { subjectId: open.subject_id, topicId: open.topic_id, sessionId: open.open_session_id };
    }
  }
  return undefined;
}

function goTo(path: string) {
  window.location.assign(path);
}

function TopicRow({ topic }: { topic: Topic }) {
  const details: string[] = [];
  if (topic.last_session_at_ms !== undefined) {
    details.push(`Última sesión: ${formatDate(topic.last_session_at_ms)}`);
  }
  if (topic.pending_count !== undefined && topic.pending_count > 0) {
    details.push(pendingText(topic.pending_count));
  }
  return (
    <li className="desk-topic">
      <a className="desk-topic-name" href={topicEntryPath(topic)}>
        {topic.name}
      </a>
      {topic.open_session_id !== undefined && (
        <>
          {" · "}
          <a className="desk-live" href={topicWorkspacePath(topic)}>
            Sesión abierta
          </a>
        </>
      )}
      {details.length > 0 && <span className="desk-topic-details"> · {details.join(" · ")}</span>}
      <span className="desk-topic-links">
        <a href={topicStudyPath(topic)}>Estudiar</a>
        <a href={topicCardPath(topic)}>Ficha</a>
      </span>
    </li>
  );
}

function SubjectSection({
  subject,
  topics,
  onTopicCreated,
}: {
  subject: Subject;
  topics: ReadResult<Topic[]>;
  onTopicCreated: (topic: Topic) => void;
}) {
  return (
    <section className="desk-subject" aria-label={subject.name}>
      <h2>{subject.name}</h2>
      <p className="desk-subject-guide">
        <a href={styleGuidePagePath(subject.subject_id)}>Guía de estilo</a>
      </p>
      {topics.kind !== "ok" && (
        <p role="alert">No se pudieron cargar los temas: {describeFailure(topics)}</p>
      )}
      {topics.kind === "ok" && topics.value.length === 0 && <p>Esta asignatura todavía no tiene temas.</p>}
      {topics.kind === "ok" && topics.value.length > 0 && (
        <ul className="desk-topics">
          {topics.value.map((topic) => (
            <TopicRow key={topic.topic_id} topic={topic} />
          ))}
        </ul>
      )}
      <NewTopic subject={subject} onCreated={onTopicCreated} />
    </section>
  );
}

/** `navigate` is where a created topic goes (the browser's location by default; tests pass their own). */
export default function App({ navigate = goTo }: { navigate?: (path: string) => void }) {
  const [desk, setDesk] = useState<Desk>({ state: "loading" });

  const addSubject = (subject: Subject) =>
    setDesk((current) =>
      current.state === "ok"
        ? { state: "ok", subjects: [...current.subjects, { subject, topics: { kind: "ok", value: [] } }] }
        : current,
    );
  const openTopic = (topic: Topic) => navigate(topicWorkspacePath(topic));

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const subjects = await fetchSubjects();
      if (subjects.kind !== "ok") {
        if (!cancelled) {
          setDesk({ state: "failed", message: `No se pudo cargar la mesa de estudio: ${describeFailure(subjects)}` });
        }
        return;
      }
      const loaded = await Promise.all(
        subjects.value.map(async (subject) => ({ subject, topics: await fetchTopics(subject.subject_id) })),
      );
      if (!cancelled) setDesk({ state: "ok", subjects: loaded });
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="desk">
      <h1>Mesa de estudio</h1>
      <div className="desk-today">
        <DeskCost session={openSession(desk)} />
        <DeskPractice />
      </div>
      {desk.state === "loading" && <p>Cargando asignaturas…</p>}
      {desk.state === "failed" && <p role="alert">{desk.message}</p>}
      {desk.state === "ok" && desk.subjects.length === 0 && (
        <p>Todavía no hay asignaturas. Crea la primera en «Nueva asignatura» y dale un tema.</p>
      )}
      {desk.state === "ok" && desk.subjects.length > 0 && (
        <div className="desk-subjects">
          {desk.subjects.map(({ subject, topics }) => (
            <SubjectSection key={subject.subject_id} subject={subject} topics={topics} onTopicCreated={openTopic} />
          ))}
        </div>
      )}
      {desk.state === "ok" && <NewSubject onCreated={addSubject} />}
    </main>
  );
}
