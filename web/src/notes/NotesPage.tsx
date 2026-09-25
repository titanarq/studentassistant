import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeFailure, fetchTopics, type ReadResult, topicPath } from "../desk/api";
import { fetchNotes, type TopicNotes } from "./api";
import { parseNotes } from "./markdown";
import NotesView from "./NotesView";
import SourcePanel from "./SourcePanel";
import "./notes.css";

/**
 * `/subjects/<subject>/topics/<topic>/notes`: the master notes of a topic (`GET .../notes`,
 * #38), rendered by `NotesView`, with the sources panel (`SourcePanel`) beside them for the
 * footnote last activated. The panel floats over the page edge (a bottom sheet at phone width),
 * so opening, switching or closing it never reflows the notes nor scrolls them: the reading
 * position stays where it was, and closing gives the focus back to the reference that opened it.
 */
export default function NotesPage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [topicName, setTopicName] = useState(topicId);
  const [notes, setNotes] = useState<ReadResult<TopicNotes> | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const trigger = useRef<HTMLElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    fetchNotes(subjectId, topicId).then((result) => {
      if (!cancelled) setNotes(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  const tree = useMemo(() => (notes?.kind === "ok" ? parseNotes(notes.value.text) : null), [notes]);

  // A link to a section (`#causas`) works once the notes are rendered.
  useEffect(() => {
    if (tree === null || window.location.hash.length < 2) return;
    const target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
    target?.scrollIntoView?.();
  }, [tree]);

  const openSource = useCallback((label: string, element: HTMLElement) => {
    trigger.current = element;
    setOpen(label);
  }, []);

  const close = useCallback(() => {
    setOpen(null);
    trigger.current?.focus({ preventScroll: true });
  }, []);

  const definition = open === null ? undefined : tree?.footnotes.find((f) => f.label === open)?.text;

  return (
    <div className={open === null ? "notes-page" : "notes-page notes-page-with-panel"}>
      <main>
        <p>
          <a href={topicPath(subjectId, topicId)}>← Tema {topicName}</a>
        </p>
        <p className="notes-meta">
          Apuntes de {topicName}
          {notes?.kind === "ok" && notes.value.version !== null && ` · versión ${notes.value.version}`}
        </p>
        {notes === null && <p>Cargando los apuntes…</p>}
        {notes !== null && notes.kind === "not-found" && <p>{notes.detail}</p>}
        {notes !== null && notes.kind !== "ok" && notes.kind !== "not-found" && (
          <p role="alert">No se pudieron cargar los apuntes: {describeFailure(notes)}</p>
        )}
        {tree !== null && <NotesView tree={tree} onOpenSource={openSource} activeLabel={open} />}
      </main>
      {open !== null && (
        <SourcePanel subjectId={subjectId} topicId={topicId} label={open} definition={definition} onClose={close} />
      )}
    </div>
  );
}
