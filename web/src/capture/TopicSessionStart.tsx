/**
 * The capture page's first step when the subject and the topic are already chosen (#312, the
 * study workspace's **Captura** tab): no picker, just the topic's session -- resumed when the
 * topic has an `open_session_id`, started otherwise, exactly as `SessionPicker` does it.
 */

import { useEffect, useState } from "react";
import type { Topic } from "../protocol";
import { listSubjects, listTopics, resumeSession, startSession } from "./api";
import { describeFailure } from "./failures";
import type { OpenedSession } from "./SessionPicker";
import "./capture.css";

export interface TopicSessionStartProps {
  subjectId: string;
  topicId: string;
  onSession: (opened: OpenedSession) => void;
  now?: () => number;
}

type TopicState =
  | { state: "loading" }
  | { state: "ready"; topic: Topic; subjectName: string }
  | { state: "missing" }
  | { state: "failed"; message: string };

type OpeningState = { state: "idle" } | { state: "opening" } | { state: "failed"; message: string };

export default function TopicSessionStart({ subjectId, topicId, onSession, now = Date.now }: TopicSessionStartProps) {
  const [topic, setTopic] = useState<TopicState>({ state: "loading" });
  const [opening, setOpening] = useState<OpeningState>({ state: "idle" });

  useEffect(() => {
    let cancelled = false;
    void Promise.all([listTopics(subjectId), listSubjects()]).then(([topics, subjects]) => {
      if (cancelled) return;
      if (topics.kind !== "ok") {
        setTopic({ state: "failed", message: describeFailure("No se ha podido cargar el tema", topics) });
        return;
      }
      const found = topics.value.topics.find((t) => t.topic_id === topicId);
      if (found === undefined) {
        setTopic({ state: "missing" });
        return;
      }
      const subjectName =
        subjects.kind === "ok" ? (subjects.value.subjects.find((s) => s.subject_id === subjectId)?.name ?? subjectId) : subjectId;
      setTopic({ state: "ready", topic: found, subjectName });
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  async function open(chosen: Topic, subjectName: string) {
    const unended = chosen.open_session_id;
    setOpening({ state: "opening" });
    const result =
      unended === undefined ? await startSession(chosen.subject_id, chosen.topic_id, now()) : await resumeSession(unended);
    if (result.kind === "ok") {
      setOpening({ state: "idle" });
      onSession({ session: result.value, subjectName, topicName: chosen.name });
      return;
    }
    const prefix = unended === undefined ? "No se ha podido empezar la sesión" : "No se ha podido continuar la sesión";
    setOpening({ state: "failed", message: describeFailure(prefix, result) });
  }

  return (
    <section className="capture-step capture-start" aria-label="Sesión">
      <h2>Capturar</h2>
      {topic.state === "loading" && <p>Cargando el tema…</p>}
      {topic.state === "missing" && <p role="alert">Este tema no existe.</p>}
      {topic.state === "failed" && <p role="alert">{topic.message}</p>}
      {topic.state === "ready" && (
        <>
          <p>
            {topic.topic.open_session_id === undefined
              ? "Este tema no tiene ninguna sesión sin terminar."
              : "Este tema tiene una sesión sin terminar: puedes continuarla donde la dejaste."}
          </p>
          <button
            type="button"
            className="capture-primary"
            disabled={opening.state === "opening"}
            onClick={() => void open(topic.topic, topic.subjectName)}
          >
            {opening.state === "opening"
              ? "Abriendo la sesión…"
              : topic.topic.open_session_id === undefined
                ? "Empezar una sesión nueva"
                : "Continuar la sesión abierta"}
          </button>
          {opening.state === "failed" && <p role="alert">{opening.message}</p>}
        </>
      )}
    </section>
  );
}
