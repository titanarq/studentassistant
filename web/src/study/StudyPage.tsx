import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeFailure, fetchTopics, type ReadResult, topicPath } from "../desk/api";
import { fetchNotes, type TopicNotes } from "../notes/api";
import { useNotesImages } from "../notes/useNotesImages";
import { type Inline, type NotesTree, parseInline, parseNotes } from "../notes/markdown";
import NotesView from "../notes/NotesView";
import SourcePanel from "../notes/SourcePanel";
import { fetchStudyState, type StudyState } from "./api";
import OptionContent from "./OptionContent";
import OptionPanel, { OPTION_PANEL_ID } from "./OptionPanel";
import { type OptionKey, STATE_LABELS, type StudyOption, studyOptions } from "./options";
import type { GenerationResult } from "./chat/api";
import StudyChat from "./chat/StudyChat";
import type { VoiceQuestionStarter } from "../tutor/voiceQuestion";
import WorkspaceFrame, { type NarrowView } from "../workspace/WorkspaceFrame";
import "../notes/notes.css";
import "./study.css";

/** What the single column shows below 900 px (the frame's views, #487). */
export type StudyView = NarrowView;

const VIEWS: Record<StudyView, string> = { document: "Documento", left: "Estudiar", chat: "Chat" };

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
        <span className="study-option-title">{option.title}</span>{" "}
        <span className={badge} title={option.staleReason ?? undefined}>
          {STATE_LABELS[option.state]}
        </span>
      </button>
    </li>
  );
}

/**
 * `/subjects/<subject>/topics/<topic>/study`, "Estudiar" (#333, #337, epic #332): the study screen
 * of a topic, in the same frame as **Construir** since #487 (`WorkspaceFrame`: the header band with
 * the switch Construir · Estudiar and the topic's name, the desk, the left card above the chat card,
 * the document card on the right, one viewport high from 900 px on). The left card holds the study
 * options as cards two per row, each with its title and its state only (#509; the flashcards'
 * reviews are reached through «Tarjetas de memoria»), under "Material de estudio" and
 * the "versión de estudio" (`GET .../study`, #335: which notes version it is and whether the notes
 * changed after it); the chat card holds the question chat (`StudyChat`, #336), with the workspace
 * chat's input and microphone, whose citation chips scroll to and highlight a section or open a
 * source; the right card the document read-only with its pinned header (**Editar en Construir**),
 * its images drawn from the topic's sources (#509).
 * Opening an option slides `OptionPanel` over the right edge of the document's body, below the
 * header, with the existing page embedded and its own pinned header, and the sections the item
 * shown is about are highlighted in the document and scrolled to. A provenance footnote opens its
 * source in the same place. Below 900 px the columns become one, with the switch Documento |
 * Estudiar | Chat.
 *
 * A material generated from the chat («hazme un quiz», #366, #367) replaces the study state with
 * the one its `result` carries (the badges change without a reload), and its **Abrir «…»** opens
 * that option's panel, reloading its content when it was already open. The phrase of an option's
 * hint («Pídelo en el chat: …») fills the chat's input (and shows the chat in one column).
 */
export default function StudyPage({
  subjectId,
  topicId,
  listen,
  voiceSupported,
}: {
  subjectId: string;
  topicId: string;
  /** The study chat's microphone: listens for one question (the Web Speech API by default). */
  listen?: VoiceQuestionStarter;
  /** Whether `listen` can work here; asked of the browser by default. */
  voiceSupported?: boolean;
}) {
  const [topicName, setTopicName] = useState(topicId);
  const [notes, setNotes] = useState<ReadResult<TopicNotes> | null>(null);
  const [study, setStudy] = useState<ReadResult<StudyState> | null>(null);
  const [openKey, setOpenKey] = useState<OptionKey | null>(null);
  const [source, setSource] = useState<{
    label: string;
    definition: string | undefined;
    /** Opened by a chip of the question chat: the blocks citing it are highlighted too. */
    fromChat?: boolean;
  } | null>(null);
  const [focus, setFocus] = useState<string[]>([]);
  const [view, setView] = useState<StudyView>("left");
  /** Bumped to remount the open option's content, so it reads the material again. */
  const [reload, setReload] = useState(0);
  const [suggestion, setSuggestion] = useState<{ text: string; id: number } | null>(null);
  const buttons = useRef(new Map<OptionKey, HTMLButtonElement>());
  const sourceTrigger = useRef<HTMLElement | null>(null);
  const pendingFocus = useRef<HTMLElement | null>(null);
  const documentRef = useRef<HTMLDivElement>(null);
  const reads = useRef(0);
  const resolveImage = useNotesImages(subjectId, topicId);

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
    void fetchStudyState(subjectId, topicId).then((result) => {
      if (read === reads.current) setStudy(result);
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

  const studyState = study?.kind === "ok" ? study.value : null;
  const options = studyOptions(studyState);
  const label = studyState?.studyVersion ?? null;
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
    setView("left");
  }, [openKey]);

  // A material generated from the chat: its state comes with the result (any read in course is
  // dropped); an open option showing that material reads it again.
  const onGenerated = useCallback(
    (result: GenerationResult) => {
      if (result.study !== null) {
        reads.current += 1;
        setStudy({ kind: "ok", value: result.study });
      }
      const shown = options.find((option) => option.key === openKey);
      if (shown !== undefined && shown.kind === result.materialKind) setReload((n) => n + 1);
    },
    [options, openKey],
  );

  // **Abrir «…»** of a generation turn: as a click in the list, and the content read again.
  const openGenerated = useCallback(
    (key: OptionKey) => {
      if (key === openKey) setReload((n) => n + 1);
      openOption(key);
    },
    [openKey, openOption],
  );

  const askInChat = useCallback((phrase: string) => {
    setSuggestion((now) => ({ text: phrase, id: (now?.id ?? 0) + 1 }));
    setView("chat");
  }, []);

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

  const studyLabel = label !== null && (
    <div className="study-label">
      <p>
        Apuntes v{label.version} · <span className="study-label-badge">versión de estudio</span>
      </p>
      {!studyState?.studyCurrent && (
        <p className="study-label-note">Has cambiado los apuntes después de la versión de estudio (v{label.version}).</p>
      )}
    </div>
  );

  return (
    <WorkspaceFrame
      mode="study"
      subjectId={subjectId}
      topicId={topicId}
      topicName={topicName}
      label="Estudiar"
      className="study"
      views={VIEWS}
      view={view}
      onView={setView}
      leftLabel="Opciones de estudio"
      leftClassName="study-card"
      left={
        <div className="study-card-body">
          <section className="study-options" aria-labelledby="study-options-heading">
            <h2 id="study-options-heading">Material de estudio</h2>
            {studyLabel}
            {study === null && <p>Cargando el material…</p>}
            {study !== null && study.kind !== "ok" && (
              <p role="alert">No se pudo leer el estado del material: {describeFailure(study)}</p>
            )}
            {study !== null && (
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
        </div>
      }
      chat={
        <StudyChat
          subjectId={subjectId}
          topicId={topicId}
          sections={titles}
          hasNotes={notes === null ? null : notes.kind !== "not-found"}
          onOpenSection={openChatSection}
          onOpenSource={openChatSource}
          onGenerated={onGenerated}
          onOpenOption={openGenerated}
          suggestion={suggestion}
          listen={listen}
          voiceSupported={voiceSupported}
        />
      }
      documentClassName="study-right"
      documentPanel={overlay}
      document={
        <>
          <div className="workspace-document-header">
            <h2 className="workspace-document-title">Apuntes</h2>
            {notes?.kind === "ok" && notes.value.version !== null && (
              <span className="workspace-document-version study-version">v{notes.value.version}</span>
            )}
            <a className="study-edit" href={workspace}>
              Editar en Construir
            </a>
          </div>
          {/* The document's body scrolls under its header; a material or a source slides in over
              its right edge, below the header, with its own pinned header (#487). */}
          <div className="study-document-area">
            <div className="workspace-document-body study-document" ref={documentRef}>
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
                  resolveImage={resolveImage}
                />
              )}
            </div>
            {open !== null && (
              <OptionPanel title={open.title} onClose={closeOption} hidden={source !== null}>
                <OptionContent
                  key={`${open.key}-${reload}`}
                  subjectId={subjectId}
                  topicId={topicId}
                  option={open}
                  onFocusAnchors={setFocus}
                  anchorLabel={anchorLabel}
                  onAskInChat={askInChat}
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
          </div>
        </>
      }
    />
  );
}
