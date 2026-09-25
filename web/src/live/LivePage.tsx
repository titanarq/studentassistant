import { useEffect, useReducer, useState } from "react";
import { fetchTopics, topicPath } from "../desk/api";
import { sourceUrl } from "../notes/api";
import { formatTimestamp } from "../notes/SourcePanel";
import {
  INITIAL_STATE,
  type LiveCapture,
  type LiveSourceFactory,
  type OutlineNode,
  contextLabel,
  defaultSource,
  outlineTree,
  reduceLive,
  statusLabel,
  subscribeLive,
} from "./live";
import "./live.css";

/**
 * `/live`: the live session view (#57). While a session runs, the PC browser follows its
 * transcript, the pages captured so far and the outline the observer is building, over the
 * backend's `GET /api/live` stream. Read-only.
 */

function pendingText(count: number): string {
  return count === 1 ? "1 duda por revisar" : `${count} dudas por revisar`;
}

function fragmentsText(count: number): string {
  return count === 1 ? "1 fragmento" : `${count} fragmentos`;
}

function Outline({ nodes }: { nodes: OutlineNode[] }) {
  return (
    <ul>
      {nodes.map(({ section, children }) => (
        <li key={section.section_id}>
          {section.title}
          {section.segment_count > 0 && <span className="live-muted"> · {fragmentsText(section.segment_count)}</span>}
          {children.length > 0 && <Outline nodes={children} />}
        </li>
      ))}
    </ul>
  );
}

function CaptureCard({ capture, index }: { capture: LiveCapture; index: number }) {
  const name =
    capture.page_number !== null
      ? `${contextLabel(capture.source_context)}, página ${capture.page_number}`
      : `${contextLabel(capture.source_context)} ${index + 1}`;
  return (
    <li className={`live-capture live-capture-${capture.status}`}>
      <article aria-label={name}>
        <header>
          <strong>{name}</strong>
          {capture.t !== null && <span className="live-muted"> · {formatTimestamp(capture.t)}</span>}
        </header>
        {capture.page_path !== null && (
          <img src={sourceUrl(capture.page_path)} alt={`Imagen de ${name}`} loading="lazy" />
        )}
        <p className="live-capture-status">{statusLabel(capture)}</p>
        {capture.text !== null && capture.text.trim() !== "" && (
          <details>
            <summary>Transcripción</summary>
            <pre>{capture.text}</pre>
          </details>
        )}
      </article>
    </li>
  );
}

export default function LivePage({ source = defaultSource }: { source?: LiveSourceFactory }) {
  const [state, dispatch] = useReducer(reduceLive, INITIAL_STATE);
  const [connected, setConnected] = useState(true);
  const [topicName, setTopicName] = useState<string | null>(null);

  useEffect(() => subscribeLive(dispatch, setConnected, source), [source]);

  const subjectId = state.session?.subject_id;
  const topicId = state.session?.topic_id;
  useEffect(() => {
    if (subjectId === undefined || topicId === undefined) return;
    let cancelled = false;
    setTopicName(null);
    fetchTopics(subjectId).then((topics) => {
      if (cancelled || topics.kind !== "ok") return;
      setTopicName(topics.value.find((t) => t.topic_id === topicId)?.name ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  const session = state.session;
  const tree = outlineTree(state.outline);

  return (
    <main className="live-page">
      <p>
        <a href="/">← Mesa de estudio</a>
      </p>
      <h1>Sesión en directo</h1>
      <div role="status">
        {state.phase === "connecting" && <p>Conectando con el servidor…</p>}
        {state.phase === "idle" && (
          <p>No hay ninguna sesión en marcha. Esta página se actualizará sola cuando empiece una.</p>
        )}
        {state.phase === "ended" && <p>La sesión ha terminado.</p>}
        {!connected && <p>Se ha perdido la conexión con el servidor; reintentando…</p>}
      </div>
      {session !== null && (
        <>
          <p>
            Tema{" "}
            <a href={topicPath(session.subject_id, session.topic_id)}>{topicName ?? session.topic_id}</a>
            {state.phase === "live" && " · en marcha"}
          </p>
          <div className="live-columns">
            <section aria-label="Transcripción" className="live-transcript">
              <h2>Transcripción</h2>
              {state.segments.length === 0 && state.partial === null && <p>Todavía no se ha dicho nada.</p>}
              <ol role="log" aria-live="polite">
                {state.segments.map((segment) => (
                  <li key={segment.segment_id}>
                    <span className="live-muted">{formatTimestamp(segment.t_start)}</span> {segment.text}
                  </li>
                ))}
              </ol>
              {state.partial !== null && (
                <p className="live-partial">
                  <span className="live-muted">{formatTimestamp(state.partial.t_start)}</span> {state.partial.text}
                </p>
              )}
            </section>
            <section aria-label="Esquema" className="live-outline">
              <h2>Esquema</h2>
              <p aria-live="polite">{pendingText(state.openPending)}</p>
              {tree.length === 0 ? <p>El observador todavía no ha propuesto un esquema.</p> : <Outline nodes={tree} />}
            </section>
          </div>
          <section aria-label="Páginas capturadas">
            <h2>Páginas capturadas</h2>
            {state.captures.length === 0 ? (
              <p>Todavía no se ha capturado ninguna página.</p>
            ) : (
              <ul className="live-captures">
                {state.captures.map((capture, index) => (
                  <CaptureCard key={capture.capture_id} capture={capture} index={index} />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </main>
  );
}
