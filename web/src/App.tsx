import { useEffect, useState } from "react";
import {
  describeFailure,
  fetchSubjects,
  fetchTopics,
  formatDate,
  type ReadResult,
  topicPath,
} from "./desk/api";
import type { Subject, Topic } from "./protocol";

/**
 * `/`: the study desk (docs/VISION.md §2). Every subject with its topics; each topic links to
 * its page (`/subjects/<s>/topics/<t>`, the topic card) and shows what the topic list carries:
 * an open session, the last session's date and the doubts waiting for review.
 */

type Desk =
  | { state: "loading" }
  | { state: "failed"; message: string }
  | { state: "ok"; subjects: { subject: Subject; topics: ReadResult<Topic[]> }[] };

function pendingText(count: number): string {
  return count === 1 ? "1 duda por revisar" : `${count} dudas por revisar`;
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
    <li>
      <a href={topicPath(topic.subject_id, topic.topic_id)}>{topic.name}</a>
      {topic.open_session_id !== undefined && (
        <>
          {" · "}
          <a href="/live">Sesión abierta</a>
        </>
      )}
      {details.length > 0 && <> · {details.join(" · ")}</>}
    </li>
  );
}

function SubjectSection({ subject, topics }: { subject: Subject; topics: ReadResult<Topic[]> }) {
  return (
    <section aria-label={subject.name}>
      <h2>{subject.name}</h2>
      {topics.kind !== "ok" && (
        <p role="alert">No se pudieron cargar los temas: {describeFailure(topics)}</p>
      )}
      {topics.kind === "ok" && topics.value.length === 0 && <p>Esta asignatura todavía no tiene temas.</p>}
      {topics.kind === "ok" && topics.value.length > 0 && (
        <ul>
          {topics.value.map((topic) => (
            <TopicRow key={topic.topic_id} topic={topic} />
          ))}
        </ul>
      )}
    </section>
  );
}

export default function App() {
  const [desk, setDesk] = useState<Desk>({ state: "loading" });

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
    <main>
      <h1>Mesa de estudio</h1>
      {desk.state === "loading" && <p>Cargando asignaturas…</p>}
      {desk.state === "failed" && <p role="alert">{desk.message}</p>}
      {desk.state === "ok" && desk.subjects.length === 0 && (
        <p>Todavía no hay asignaturas. Aparecerán aquí cuando empieces tu primera sesión de estudio.</p>
      )}
      {desk.state === "ok" &&
        desk.subjects.map(({ subject, topics }) => (
          <SubjectSection key={subject.subject_id} subject={subject} topics={topics} />
        ))}
    </main>
  );
}
