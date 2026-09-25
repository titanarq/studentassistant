import { useEffect, useState } from "react";
import { describeFailure, fetchTopics, type ReadResult, topicPath } from "../desk/api";
import { fetchPending, KIND_LABELS, kindLabel, type PendingFilter, type PendingItem, type TopicPending } from "./api";
import PendingCard from "./PendingCard";
import "./pending.css";

/** How often the queue is read again while the page is visible, so the counts stay live. */
export const POLL_MS = 5000;

const FILTERS: { value: PendingFilter; label: string }[] = [
  { value: "open", label: "Por revisar" },
  { value: "closed", label: "Cerradas" },
  { value: "all", label: "Todas" },
];

const KIND_ORDER = Object.keys(KIND_LABELS);

function rank(kind: string): number {
  const index = KIND_ORDER.indexOf(kind);
  return index === -1 ? KIND_ORDER.length : index;
}

/** The items grouped by kind (known kinds in `KIND_LABELS` order, then any other), stable. */
export function byKind(items: PendingItem[]): PendingItem[][] {
  const groups = new Map<string, PendingItem[]>();
  for (const item of items) {
    const group = groups.get(item.kind);
    if (group) group.push(item);
    else groups.set(item.kind, [item]);
  }
  return [...groups.entries()].sort(([a], [b]) => rank(a) - rank(b)).map(([, group]) => group);
}

function countLine(open: number): string {
  if (open === 0) return "Nada por revisar";
  return open === 1 ? "1 duda por revisar" : `${open} dudas por revisar`;
}

/**
 * `/subjects/<subject>/topics/<topic>/pending`: the topic's pending-review queue (`GET
 * .../pending`, #55) as cards grouped by kind, with a filter (to review, closed, all). The queue
 * is read again every `pollMs` while the page is visible, so the count and the cards follow a
 * session in progress. Answering or dismissing a doubt from here needs the editor's doubts API
 * (#68).
 */
export default function PendingPage({
  subjectId,
  topicId,
  pollMs = POLL_MS,
}: {
  subjectId: string;
  topicId: string;
  pollMs?: number;
}) {
  const [topicName, setTopicName] = useState(topicId);
  const [filter, setFilter] = useState<PendingFilter>("open");
  const [queue, setQueue] = useState<ReadResult<TopicPending> | null>(null);
  const [stale, setStale] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    setQueue(null);
    setStale(false);
    const load = (first: boolean) => {
      if (inFlight) return;
      inFlight = true;
      fetchPending(subjectId, topicId, filter).then((result) => {
        inFlight = false;
        if (cancelled) return;
        if (result.kind === "ok" || first) {
          setQueue(result);
          setStale(false);
        } else {
          // Keep what is shown; say it may be out of date.
          setQueue((shown) => (shown?.kind === "ok" ? shown : result));
          setStale(true);
        }
      });
    };
    load(true);
    const timer = window.setInterval(() => {
      if (!document.hidden) load(false);
    }, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [subjectId, topicId, filter, pollMs]);

  const topicHref = topicPath(subjectId, topicId);

  return (
    <main className="pending-page">
      <p>
        <a href={topicHref}>← Tema {topicName}</a>
      </p>
      <h1>Dudas pendientes</h1>
      {queue?.kind === "ok" && (
        <p className="pending-count" aria-live="polite">
          {countLine(queue.value.open_count)}
        </p>
      )}
      <div role="group" aria-label="Qué dudas mostrar" className="pending-filters">
        {FILTERS.map(({ value, label }) => (
          <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>
            {label}
          </button>
        ))}
      </div>
      {stale && <p className="pending-stale">No se pudo actualizar la lista; se muestra la última recibida.</p>}
      {queue === null && <p>Cargando las dudas…</p>}
      {queue !== null && queue.kind !== "ok" && (
        <p role="alert">No se pudieron cargar las dudas: {describeFailure(queue)}</p>
      )}
      {queue?.kind === "ok" && queue.value.items.length === 0 && (
        <p>{filter === "closed" ? "Todavía no se ha cerrado ninguna duda." : "No hay dudas que mostrar."}</p>
      )}
      {queue?.kind === "ok" &&
        byKind(queue.value.items).map((group) => (
          <section key={group[0].kind} aria-label={`${kindLabel(group[0].kind)} (${group.length})`}>
            {group.map((item) => (
              <PendingCard key={item.id} item={item} />
            ))}
          </section>
        ))}
      {queue?.kind === "ok" && queue.value.open_count > 0 && (
        <p className="pending-note">Pronto podrás responder o descartar cada duda desde aquí.</p>
      )}
    </main>
  );
}
