import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeFailure, fetchTopics, type ReadResult, topicPath } from "../desk/api";
import { fetchMaterials, type Materials } from "../materials/api";
import { fetchNotes, type TopicNotes } from "../notes/api";
import { type Inline, type NotesTree, parseInline, parseNotes } from "../notes/markdown";
import NotesView from "../notes/NotesView";
import SourcePanel from "../notes/SourcePanel";
import ModeSwitch from "./ModeSwitch";
import OptionContent from "./OptionContent";
import OptionPanel, { OPTION_PANEL_ID } from "./OptionPanel";
import { type OptionKey, STATE_LABELS, type StudyOption, studyOptions } from "./options";
import ReviewsToday from "./ReviewsToday";
import StudyChat from "./chat/StudyChat";
import "../notes/notes.css";
import "./study.css";

/** What the single column shows below 900 px. */
export type StudyView = "study" | "document";

const VIEWS: Array<[StudyView, string]> = [
  ["study", "Estudiar"],
  ["document", "Documento"],
];

function plain(nodes: Inline[]): string {
  return nodes
    .map((node) => {
      if (node.type === "footnote") return "";
      if ("children" in node) return plain(node.children);
      return "text" in node ? node.text : "";
    })
    .join("");
}

/** Anchor -> the section's title as text, for every heading with an anchor. */
export function sectionTitles(tree: NotesTree | null): Map<string, string> {
  const titles = new Map<string, string>();
  for (const block of tree?.blocks ?? []) {
    if (block.type === "heading" && block.anchor !== null) {
      titles.set(block.anchor, plain(parseInline(block.text)).trim() || block.anchor);
    }
  }
  return titles;
}

function OptionButton({
  option,
  open,
  register,
  onToggle,
}: {
  option: StudyOption;
  open: boolean;
  register: (key: OptionKey, element: HTMLButtonElement | null) => void;
  onToggle: (key: OptionKey) => void;
}) {
  const badge =
    option.state === "ready" ? "badge badge-ok" : option.state === "stale" ? "badge badge-warn" : "study-missing-badge";
  return (
    <li>
      <button
        type="button"
        ref={(element) => register(option.key, element)}
        className="study-option"
        aria-expanded={open}
        aria-controls={open ? OPTION_PANEL_ID : undefined}
        onClick={() => onToggle(option.key)}
      >
        <span className="study-option-text">
          <span className="study-option-title">{option.title}</span>
          <span className="study-option-description">{option.description}</span>
        </span>
        <span className={badge} title={option.staleReason ?? undefined}>
          {STATE_LABELS[option.state]}
        </span>
      </button>
    </li>
  );
}

/**
 * `/subjects/<subject>/topics/<topic>/study`, "Estudiar" (#333, epic #332): the study screen of a
 * topic. Left, "Repasos para hoy" for the topic, the study options with their state and the
 * question chat (`StudyChat`, #336), whose citation chips scroll to and highlight a section or open a source; right, the document read-only with "Editar en Construir". Opening an option
 * slides `OptionPanel` over the right edge of the document with the existing page embedded, and
 * the sections the item shown is about are highlighted in the document and scrolled to. A
 * provenance footnote opens its source in the same place. Below 900 px the columns become one,
 * with the switch Estudiar | Documento.
 */
export default function StudyPage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [topicName, setTopicName] = useState(topicId);
  const [notes, setNotes] = useState<ReadResult<TopicNotes> | null>(null);
  const [materials, setMaterials] = useState<ReadResult<Materials> | null>(null);
  const [openKey, setOpenKey] = useState<OptionKey | null>(null);
  const [source, setSource] = useState<{
    label: string;
    definition: string | undefined;
    /** Opened by a chip of the question chat: the blocks citing it are highlighted too. */
    fromChat?: boolean;
  } | null>(null);
  const [focus, setFocus] = useState<string[]>([]);
  const [view, setView] = useState<StudyView>("study");
  const buttons = useRef(new Map<OptionKey, HTMLButtonElement>());
  const sourceTrigger = useRef<HTMLElement | null>(null);
  const pendingFocus = useRef<HTMLElement | null>(null);
  const documentRef = useRef<HTMLDivElement>(null);
  const reads = useRef(0);

  useEffect(() => {
    let cancelled = false;
    void fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    void fetchNotes(subjectId, topicId).then((result) => {
      if (!cancelled) setNotes(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  // Only the latest read is shown, so an older answer never overwrites a newer state.
  useEffect(() => {
    const read = ++reads.current;
    void fetchMaterials(subjectId, topicId).then((result) => {
      if (read === reads.current) setMaterials(result);
    });
  }, [subjectId, topicId]);

  const tree = useMemo(() => (notes?.kind === "ok" ? parseNotes(notes.value.text) : null), [notes]);
  const titles = useMemo(() => sectionTitles(tree), [tree]);
  const anchorLabel = useCallback((anchor: string) => titles.get(anchor) ?? anchor, [titles]);
  const focusSections = useMemo(() => new Set(focus.filter((anchor) => titles.has(anchor))), [focus, titles]);

  // Brings the element of the document with that `id` into view, at the top of the document.
  const scrollDocumentTo = useCallback((id: string) => {
    const container = documentRef.current;
    const target = [...(container?.querySelectorAll<HTMLElement>("[id]") ?? [])].find((e) => e.id === id);
    // Only the document scrolls (never the page), so the options stay where they are.
    if (container === null || target === undefined || typeof container.scrollTo !== "function") return;
    const top = target.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
    container.scrollTo({ top: Math.max(0, top - 16), behavior: "smooth" });
  }, []);

  // The first highlighted section comes into view.
  useEffect(() => {
    const first = focus.find((anchor) => titles.has(anchor));
    if (first !== undefined) scrollDocumentTo(first);
  }, [focus, titles, scrollDocumentTo]);

  // A source chip of the chat: the first place the notes cite it comes into view.
  const chatLabel = source?.fromChat === true ? source.label : null;
  useEffect(() => {
    if (chatLabel !== null) scrollDocumentTo(`fnref-${chatLabel}-1`);
  }, [chatLabel, scrollDocumentTo]);

  // The focus goes back to the button that opened what was just closed, once it is shown again.
  useEffect(() => {
    const element = pendingFocus.current;
    if (element === null) return;
    pendingFocus.current = null;
    if (element.isConnected) element.focus({ preventScroll: true });
  });

  const options = studyOptions(materials?.kind === "ok" ? materials.value : null);
  const open = options.find((option) => option.key === openKey) ?? null;

  const register = useCallback((key: OptionKey, element: HTMLButtonElement | null) => {
    if (element === null) buttons.current.delete(key);
    else buttons.current.set(key, element);
  }, []);

  const openOption = useCallback((key: OptionKey) => {
    setOpenKey(key);
    setSource(null);
    setFocus([]);
    setView("document");
  }, []);

  const closeOption = useCallback(() => {
    pendingFocus.current = openKey === null ? null : (buttons.current.get(openKey) ?? null);
    setOpenKey(null);
    setFocus([]);
    setView("study");
  }, [openKey]);

  const toggle = useCallback(
    (key: OptionKey) => {
      if (key === openKey && source === null) closeOption();
      else openOption(key);
    },
    [openKey, source, closeOption, openOption],
  );

  const openSource = useCallback(
    (label: string, element: HTMLElement) => {
      sourceTrigger.current = element;
      setSource({ label, definition: tree?.footnotes.find((f) => f.label === label)?.text });
    },
    [tree],
  );

  // A section chip of the chat: the document shows that section, highlighted.
  const openChatSection = useCallback((anchor: string) => {
    setSource(null);
    setFocus([anchor]);
    setView("document");
  }, []);

  // A source chip of the chat: its source opens as a footnote's does and its blocks are highlighted.
  const openChatSource = useCallback(
    (label: string, element: HTMLElement) => {
      sourceTrigger.current = element;
      setSource({ label, definition: tree?.footnotes.find((f) => f.label === label)?.text, fromChat: true });
      setView("document");
    },
    [tree],
  );

  const closeSource = useCallback(() => {
    pendingFocus.current = sourceTrigger.current;
    sourceTrigger.current = null;
    setSource(null);
  }, []);

  const base = topicPath(subjectId, topicId);
  const workspace = `${base}/workspace`;
  const overlay = open !== null || source !== null;

  return (
    <div className="study" data-view={view}>
      <header className="study-header">
        <p className="crumbs">
          <a href={base}>← Tema {topicName}</a>
          <a className="crumbs-home" href="/">
            Mesa de estudio
          </a>
        </p>
        <div className="study-title">
          <h1>Estudiar</h1>
          <ModeSwitch subjectId={subjectId} topicId={topicId} current="study" />
        </div>
        <p className="page-context">Tema {topicName}</p>
        <div className="study-switch" role="group" aria-label="Qué mostrar">
          {VIEWS.map(([key, label]) => (
            <button key={key} type="button" aria-pressed={view === key} onClick={() => setView(key)}>
              {label}
            </button>
          ))}
        </div>
      </header>
      <div className="study-columns">
        <div className="study-left">
          <ReviewsToday
            subjectId={subjectId}
            topicId={topicId}
            onReview={() => openOption("tarjetas")}
          />
          <section className="study-options" aria-labelledby="study-options-heading">
            <h2 id="study-options-heading">Material de estudio</h2>
            {materials === null && <p>Cargando el material…</p>}
            {materials !== null && materials.kind !== "ok" && (
              <p role="alert">No se pudo leer el estado del material: {describeFailure(materials)}</p>
            )}
            {materials !== null && (
              <ul className="study-option-list">
                {options.map((option) => (
                  <OptionButton
                    key={option.key}
                    option={option}
                    open={option.key === openKey}
                    register={register}
                    onToggle={toggle}
                  />
                ))}
              </ul>
            )}
          </section>
          <StudyChat
            subjectId={subjectId}
            topicId={topicId}
            sections={titles}
            hasNotes={notes === null ? null : notes.kind !== "not-found"}
            onOpenSection={openChatSection}
            onOpenSource={openChatSource}
          />
        </div>
        <section className="study-right" aria-label="Documento" data-panel={overlay ? "open" : undefined}>
          <div className="study-document" ref={documentRef}>
            <div className="study-document-bar">
              <h2>Apuntes</h2>
              {notes?.kind === "ok" && notes.value.version !== null && (
                <span className="study-version">v{notes.value.version}</span>
              )}
              <a className="study-edit" href={workspace}>
                Editar en Construir
              </a>
            </div>
            {notes === null && <p>Cargando los apuntes…</p>}
            {notes?.kind === "not-found" && (
              <p className="study-empty">
                Todavía no hay apuntes: constrúyelos en <a href={workspace}>Construir</a>.
              </p>
            )}
            {notes !== null && notes.kind !== "ok" && notes.kind !== "not-found" && (
              <p role="alert">No se pudieron cargar los apuntes: {describeFailure(notes)}</p>
            )}
            {tree !== null && (
              <NotesView
                tree={tree}
                onOpenSource={openSource}
                activeLabel={source?.label ?? null}
                focusSections={focusSections}
                focusLabel={chatLabel}
              />
            )}
          </div>
          {open !== null && (
            <OptionPanel title={open.title} onClose={closeOption} hidden={source !== null}>
              <OptionContent
                key={open.key}
                subjectId={subjectId}
                topicId={topicId}
                option={open}
                onFocusAnchors={setFocus}
                anchorLabel={anchorLabel}
              />
            </OptionPanel>
          )}
          {source !== null && (
            <SourcePanel
              subjectId={subjectId}
              topicId={topicId}
              label={source.label}
              definition={source.definition}
              onClose={closeSource}
            />
          )}
        </section>
      </div>
    </div>
  );
}
