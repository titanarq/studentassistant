import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeFailure, fetchTopics, type ReadResult, topicPath } from "../desk/api";
import { fetchNotes, type TopicNotes } from "../notes/api";
import { parseNotes } from "../notes/markdown";
import NotesView from "../notes/NotesView";
import SourcePanel from "../notes/SourcePanel";
import "../notes/notes.css";
import { KIND_LABELS, kindLabel, type PendingFilter, type PendingItem } from "./api";
import DoubtResolver from "./DoubtResolver";
import {
  type ActionResult,
  describeActionFailure,
  type Doubt,
  type DoubtsQueue,
  describeReview,
  fetchDoubts,
  type ResolutionResult,
  type ReviewResult,
  reviewDoubts,
} from "./doubts";

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

/** The doubts the filter keeps, in the queue's order (open first). */
export function applyFilter(items: Doubt[], filter: PendingFilter): Doubt[] {
  if (filter === "all") return items;
  return items.filter((doubt) => (doubt.item.status === "open") === (filter === "open"));
}

function describeResolution(result: ResolutionResult): string {
  const parts = [
    result.status === "dismissed"
      ? "Duda descartada."
      : result.resolution !== null
        ? `Duda resuelta: ${result.resolution}`
        : "Duda resuelta.",
  ];
  if (result.notes_changed) parts.push("Los apuntes se han actualizado.");
  if (result.warning !== null) parts.push(result.warning);
  return parts.join(" ");
}

function countLine(open: number): string {
  if (open === 0) return "Nada por revisar";
  return open === 1 ? "1 duda por revisar" : `${open} dudas por revisar`;
}

/**
 * `/subjects/<subject>/topics/<topic>/pending`: the topic's doubts (`GET .../doubts`, #68) as
 * cards grouped by kind, with a filter (to review, closed, all) applied here. The queue is read
 * again every `pollMs` while the page is visible, so the count and the cards follow a session in
 * progress.
 *
 * The doubts are resolved one at a time: the open doubt being answered (the queue's `current`
 * unless the student picked another) carries the answer form (`DoubtResolver`). After an answer or
 * a dismissal the queue is read again, and the topic's notes, shown under the cards, are read
 * again when the answer changed them. "Preparar las preguntas" (`POST .../doubts/review`) asks the
 * editor for the questions of open doubts that have none. Only one doubts operation runs at a time.
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
  const [queue, setQueue] = useState<ReadResult<DoubtsQueue> | null>(null);
  const [stale, setStale] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [reviewFailure, setReviewFailure] = useState<{
    result: Exclude<ActionResult<ReviewResult>, { kind: "ok" }>;
    retry: (() => void) | null;
  } | null>(null);
  const [notes, setNotes] = useState<ReadResult<TopicNotes> | null>(null);
  const [notesReload, setNotesReload] = useState(0);
  const [openSource, setOpenSource] = useState<string | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const reload = useRef<() => void>(() => undefined);

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
    let issued = 0;
    let applied = 0;
    setQueue(null);
    setStale(false);
    const load = () => {
      const seq = ++issued;
      fetchDoubts(subjectId, topicId).then((result) => {
        // A slower, older read never overwrites a newer one.
        if (cancelled || seq < applied) return;
        applied = seq;
        if (result.kind === "ok") {
          setQueue(result);
          setStale(false);
        } else {
          // Keep what is shown; say it may be out of date.
          setQueue((shown) => (shown?.kind === "ok" ? shown : result));
          setStale((shown) => shown || seq > 1);
        }
      });
    };
    reload.current = load;
    load();
    const timer = window.setInterval(() => {
      if (!document.hidden) load();
    }, pollMs);
    return () => {
      cancelled = true;
      reload.current = () => undefined;
      window.clearInterval(timer);
    };
  }, [subjectId, topicId, pollMs]);

  useEffect(() => {
    let cancelled = false;
    fetchNotes(subjectId, topicId).then((result) => {
      if (!cancelled) setNotes(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, notesReload]);

  const tree = useMemo(() => (notes?.kind === "ok" ? parseNotes(notes.value.text) : null), [notes]);

  const refreshQueue = useCallback(() => reload.current(), []);

  const resolved = useCallback(
    (result: ResolutionResult) => {
      setMessage(describeResolution(result));
      setPicked(null);
      if (result.notes_changed) setNotesReload((n) => n + 1);
      refreshQueue();
    },
    [refreshQueue],
  );

  const review = useCallback(
    async (confirm: boolean) => {
      setReviewing(true);
      setBusy(true);
      setReviewFailure(null);
      setMessage(null);
      const result = await reviewDoubts(subjectId, topicId, confirm);
      setReviewing(false);
      setBusy(false);
      if (result.kind === "ok") {
        setMessage([describeReview(result.value), result.value.warning].filter(Boolean).join(" "));
        if (result.value.notes_changed) setNotesReload((n) => n + 1);
        refreshQueue();
        return;
      }
      const overCap = result.kind === "refused" && result.overCap;
      setReviewFailure({ result, retry: overCap ? () => void review(true) : null });
    },
    [subjectId, topicId, refreshQueue],
  );

  const openFootnote = useCallback((label: string, element: HTMLElement) => {
    trigger.current = element;
    setOpenSource(label);
  }, []);

  const closeSource = useCallback(() => {
    setOpenSource(null);
    trigger.current?.focus({ preventScroll: true });
  }, []);

  const topicHref = topicPath(subjectId, topicId);
  const doubts = queue?.kind === "ok" ? queue.value : null;
  const openIds = new Set(doubts?.items.filter((d) => d.item.status === "open").map((d) => d.item.id) ?? []);
  const active = picked !== null && openIds.has(picked) ? picked : (doubts?.current ?? null);
  const shown = doubts === null ? [] : applyFilter(doubts.items, filter);
  const byId = new Map(shown.map((doubt) => [doubt.item.id, doubt]));
  const unasked = doubts?.items.some((d) => d.item.status === "open" && d.question === null) ?? false;
  const definition = openSource === null ? undefined : tree?.footnotes.find((f) => f.label === openSource)?.text;

  return (
    <main className="pending-page">
      <p className="crumbs">
        <a href={topicHref}>← Tema {topicName}</a>
        <a className="crumbs-home" href="/">
          Mesa de estudio
        </a>
      </p>
      <h1>Dudas pendientes</h1>
      {doubts !== null && (
        <p className="pending-count" aria-live="polite">
          {countLine(doubts.open_count)}
        </p>
      )}
      <div role="group" aria-label="Qué dudas mostrar" className="pending-filters">
        {FILTERS.map(({ value, label }) => (
          <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>
            {label}
          </button>
        ))}
      </div>
      {unasked && (
        <div className="pending-review">
          <button type="button" disabled={busy} onClick={() => void review(false)}>
            Preparar las preguntas
          </button>
          <span> El editor busca la respuesta en tus fuentes y te pregunta el resto.</span>
        </div>
      )}
      <div aria-live="polite" className="pending-message">
        {reviewing && <p>El editor está revisando las dudas…</p>}
        {message !== null && <p>{message}</p>}
      </div>
      {reviewFailure !== null && (
        <div role="alert" className="doubt-failure">
          <p>No se pudieron preparar las preguntas: {describeActionFailure(reviewFailure.result)}</p>
          {reviewFailure.retry !== null && (
            <button type="button" disabled={busy} onClick={reviewFailure.retry}>
              Continuar igualmente
            </button>
          )}
        </div>
      )}
      {stale && <p className="pending-stale">No se pudo actualizar la lista; se muestra la última recibida.</p>}
      {queue === null && <p>Cargando las dudas…</p>}
      {queue !== null && queue.kind !== "ok" && (
        <p role="alert">No se pudieron cargar las dudas: {describeFailure(queue)}</p>
      )}
      {doubts !== null && shown.length === 0 && (
        <p>{filter === "closed" ? "Todavía no se ha cerrado ninguna duda." : "No hay dudas que mostrar."}</p>
      )}
      {byKind(shown.map((doubt) => doubt.item)).map((group) => (
        <section key={group[0].kind} aria-label={`${kindLabel(group[0].kind)} (${group.length})`}>
          {group.map((item) => {
            const doubt = byId.get(item.id);
            const open = item.status === "open";
            return (
              <PendingCard key={item.id} item={item}>
                {open && doubt !== undefined && item.id === active && (
                  <DoubtResolver
                    key={item.id}
                    subjectId={subjectId}
                    topicId={topicId}
                    doubt={doubt}
                    disabled={busy}
                    onBusy={(working) => {
                      setBusy(working);
                      if (working) setMessage(null);
                    }}
                    onResolved={resolved}
                    onStale={refreshQueue}
                  />
                )}
                {open && item.id !== active && (
                  <button type="button" className="pending-pick" disabled={busy} onClick={() => setPicked(item.id)}>
                    Resolver esta duda
                  </button>
                )}
              </PendingCard>
            );
          })}
        </section>
      ))}
      <section aria-labelledby="pending-notes-title" className="pending-notes">
        <h2 id="pending-notes-title">
          Apuntes
          {notes?.kind === "ok" && notes.value.version !== null && ` · versión ${notes.value.version}`}
        </h2>
        {notes === null && <p>Cargando los apuntes…</p>}
        {notes !== null && notes.kind === "not-found" && <p>{notes.detail}</p>}
        {notes !== null && notes.kind !== "ok" && notes.kind !== "not-found" && (
          <p role="alert">No se pudieron cargar los apuntes: {describeFailure(notes)}</p>
        )}
        {tree !== null && <NotesView tree={tree} onOpenSource={openFootnote} activeLabel={openSource} />}
      </section>
      {openSource !== null && (
        <SourcePanel
          subjectId={subjectId}
          topicId={topicId}
          label={openSource}
          definition={definition}
          onClose={closeSource}
        />
      )}
    </main>
  );
}
