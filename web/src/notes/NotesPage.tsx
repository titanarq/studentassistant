import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import EditorChat from "../chat/EditorChat";
import { useEditorChat } from "../chat/useEditorChat";
import { blockExcerpt, whyQuestion } from "../chat/why";
import { describeFailure, fetchTopics, type ReadResult, topicPath } from "../desk/api";
import PrepareTopic from "../topic/PrepareTopic";
import { fetchNotes, type TopicNotes } from "./api";
import { type Block, parseNotes } from "./markdown";
import NotesView from "./NotesView";
import SourcePanel from "./SourcePanel";
import "./notes.css";

/**
 * `/subjects/<subject>/topics/<topic>/notes`: the master notes of a topic (`GET .../notes`,
 * #38), rendered by `NotesView`, with the sources panel (`SourcePanel`) beside them for the
 * footnote last activated. The panel floats over the page edge (a bottom sheet at phone width),
 * so opening, switching or closing it never reflows the notes nor scrolls them: the reading
 * position stays where it was, and closing gives the focus back to the reference that opened it.
 *
 * Beside the notes (below them at phone width) is the chat with the editor (`EditorChat`, #71):
 * after a turn or an undo that changed the notes they are read again, and the sections the turn
 * touched are highlighted; every block offers "¿Por qué pusiste esto?", which asks the editor
 * (`POST .../notes/why`, #69) and shows the answer in the same chat, with the block's sources:
 * each opens in the sources panel. A topic without notes yet offers "Prepárame el tema" instead.
 *
 * At phone width (below 48rem, `notes.css`) the page is one column: the notes, then the sources
 * panel (sticky to the bottom of the screen), then the chat. The sources panel and the chat are
 * collapsible there, both collapsed at first: "Fuentes" and "Chat con el editor" toggle them
 * (`aria-expanded`); a footnote expands the sources panel on the source it cites, and "¿Por qué
 * pusiste esto?" expands the chat. Wider, the toggles are hidden and nothing collapses.
 */
export default function NotesPage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [topicName, setTopicName] = useState(topicId);
  const [notes, setNotes] = useState<ReadResult<TopicNotes> | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [sourcesExpanded, setSourcesExpanded] = useState(false);
  const [chatExpanded, setChatExpanded] = useState(false);
  const [chatScroll, setChatScroll] = useState(0);
  const [changed, setChanged] = useState<ReadonlySet<string>>(new Set());
  const trigger = useRef<HTMLElement | null>(null);
  const reads = useRef(0);

  // Only the latest read is shown, so an older answer never overwrites newer notes.
  const loadNotes = useCallback(async () => {
    const read = ++reads.current;
    const result = await fetchNotes(subjectId, topicId);
    if (read === reads.current) setNotes(result);
  }, [subjectId, topicId]);

  useEffect(() => {
    let cancelled = false;
    fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    void loadNotes();
    return () => {
      cancelled = true;
      reads.current++;
    };
  }, [subjectId, topicId, loadNotes]);

  const onNotesChanged = useCallback(
    (sections: string[]) => {
      setChanged(new Set(sections));
      void loadNotes();
    },
    [loadNotes],
  );
  const chat = useEditorChat(subjectId, topicId, onNotesChanged);
  const { ask } = chat;

  const askWhy = useCallback(
    (block: Block, section: string | null, number: number) => {
      const question = whyQuestion(block, section);
      if (question === null) return;
      ask({ section, block: number, quote: blockExcerpt(block) }, question);
      setChatExpanded(true);
      setChatScroll((n) => n + 1);
    },
    [ask],
  );

  // After the chat is expanded (it may have been collapsed at phone width), so it can be scrolled to.
  useEffect(() => {
    if (chatScroll === 0) return;
    document.getElementById("editor-chat-heading")?.scrollIntoView?.({ block: "nearest" });
  }, [chatScroll]);

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
    setSourcesExpanded(true);
  }, []);

  const close = useCallback(() => {
    setOpen(null);
    setSourcesExpanded(false);
    trigger.current?.focus({ preventScroll: true });
  }, []);

  const definition = open === null ? undefined : tree?.footnotes.find((f) => f.label === open)?.text;

  return (
    <div className={open === null ? "notes-page" : "notes-page notes-page-with-panel"}>
      <main>
        <p className="crumbs">
          <a href={topicPath(subjectId, topicId)}>← Tema {topicName}</a>
          <a className="crumbs-home" href="/">
            Mesa de estudio
          </a>
        </p>
        <p className="notes-meta">
          Apuntes de {topicName}
          {notes?.kind === "ok" && notes.value.version !== null && ` · versión ${notes.value.version}`}
        </p>
        {notes === null && <p>Cargando los apuntes…</p>}
        {notes !== null && notes.kind === "not-found" && (
          <>
            <p>{notes.detail}</p>
            <PrepareTopic subjectId={subjectId} topicId={topicId} onDone={() => void loadNotes()} />
          </>
        )}
        {notes !== null && notes.kind !== "ok" && notes.kind !== "not-found" && (
          <p role="alert">No se pudieron cargar los apuntes: {describeFailure(notes)}</p>
        )}
        {tree !== null && (
          <NotesView
            tree={tree}
            onOpenSource={openSource}
            activeLabel={open}
            changedSections={changed}
            onAskWhy={askWhy}
            askDisabled={chat.busy !== null}
          />
        )}
      </main>
      {(tree !== null || open !== null) && (
        <div className="notes-sources">
          <CollapseToggle
            label="Fuentes"
            controls="notes-sources-body"
            expanded={sourcesExpanded}
            onToggle={() => setSourcesExpanded((e) => !e)}
          />
          <div id="notes-sources-body" className={collapsibleClass(sourcesExpanded)}>
            {open === null ? (
              <p className="notes-sources-hint">Toca una referencia de los apuntes para ver aquí su fuente.</p>
            ) : (
              <SourcePanel subjectId={subjectId} topicId={topicId} label={open} definition={definition} onClose={close} />
            )}
          </div>
        </div>
      )}
      {tree !== null && (
        <aside className="notes-chat" aria-label="Chat con el editor">
          <CollapseToggle
            label="Chat con el editor"
            controls="notes-chat-body"
            expanded={chatExpanded}
            onToggle={() => setChatExpanded((e) => !e)}
          />
          <div id="notes-chat-body" className={collapsibleClass(chatExpanded)}>
            <EditorChat chat={chat} onOpenSource={openSource} />
          </div>
        </aside>
      )}
    </div>
  );
}

function collapsibleClass(expanded: boolean): string {
  return expanded ? "notes-collapsible" : "notes-collapsible notes-collapsed";
}

/** A phone-width toggle (hidden wider by `notes.css`) for the sources panel or the chat. */
function CollapseToggle(props: { label: string; controls: string; expanded: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      className="notes-toggle"
      aria-expanded={props.expanded}
      aria-controls={props.controls}
      onClick={props.onToggle}
    >
      {props.label}
      <span aria-hidden="true">{props.expanded ? "▾" : "▸"}</span>
    </button>
  );
}
