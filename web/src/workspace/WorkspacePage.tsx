import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CapturePage from "../capture/CapturePage";
import { fetchTopics, topicPath } from "../desk/api";
import { parseNotes } from "../notes/markdown";
import SourcePanel from "../notes/SourcePanel";
import DocumentPanel from "./DocumentPanel";
import { SourceSelectionContext, useSourceSelection } from "./resources/selection";
import ResourcesTab, { type OpenResource } from "./ResourcesTab";
import { useWorkspaceState, WorkspaceContext } from "./state";
import WorkspaceChatSlot from "./WorkspaceChatSlot";
import WorkspaceTabs from "./WorkspaceTabs";
import ModeSwitch from "../study/ModeSwitch";
import "../notes/notes.css";
import "./workspace.css";

type Tab = "capture" | "resources";
/** What the single column shows below 900 px. */
export type NarrowView = "document" | "left" | "chat";

export { EMPTY_NOTES } from "./DocumentPanel";

const VIEWS: Array<[NarrowView, string]> = [
  ["document", "Documento"],
  ["left", "Captura/Recursos"],
  ["chat", "Chat"],
];

/**
 * `/subjects/<subject>/topics/<topic>/workspace`, "Espacio de estudio" (#312, epic #311): one
 * screen with, on the left, the tabs **Captura** (the capture flow, preset to this topic) and
 * **Recursos** (the topic's sources) above the chat, and on the right the
 * topic's document (`apuntes.md`), which the student can also edit (`DocumentPanel`, #316).
 * Choosing a source (a card in **Recursos**, a provenance footnote, a chat source link) shows its
 * detail over the document column since #473 (`SourcePanel`'s `overlay` variant: the capture large,
 * its transcription editable by hand): the document stays mounted underneath, untouched, and the
 * tabs stay where they were. The X («Cerrar») or Escape closes it and the focus goes back to what
 * opened it. The capture tab stays mounted while hidden,
 * so a running session goes on, but since #450 it is paused (camera and microphone off, the socket
 * says `pause`) while **Recursos** is shown, and resumes back on **Captura**.
 * The header (#450) is one compact bar: the switch **Construir · Estudiar** and the topic's name on
 * the left, the **Versiones** link and the way back to the study desk on the right (no doubts
 * counter and no spend line since #450: the doubts are asked in the chat).
 * The sources ticked in **Recursos** (#432) are the page's selection (`SourceSelectionContext`):
 * chips above the chat input, sent with the next message.
 * Below 900 px the columns become one, with the switch Documento | Captura/Recursos | Chat; a
 * source's detail is shown in the document view's place, and closing it goes back to the view it
 * was opened from.
 */
export default function WorkspacePage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const state = useWorkspaceState(subjectId, topicId);
  // The Recursos selection (#432), above the tabs so switching them keeps it; the chat sends it.
  const selection = useSourceSelection();
  const { notes } = state;
  const [topicName, setTopicName] = useState(topicId);
  const [tab, setTab] = useState<Tab>("capture");
  /** Since #470: the running capture is recording (the Captura tab's red dot). */
  const [recording, setRecording] = useState(false);
  const [view, setView] = useState<NarrowView>("document");
  const [capturing, setCapturing] = useState(false);
  const [open, setOpen] = useState<OpenResource | null>(null);
  const [resourcesShown, setResourcesShown] = useState(0);
  const trigger = useRef<HTMLElement | null>(null);
  /** The single-column view the detail was opened from, shown again when it closes. */
  const returnView = useRef<NarrowView>("document");
  /** Set on close: the element that gets the focus back once its view is shown again. */
  const refocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (cancelled || !topic) return;
      setTopicName(topic.name);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  const tree = useMemo(() => (notes.kind === "ready" ? parseNotes(notes.text) : null), [notes]);

  const changeTab = useCallback((next: Tab) => {
    setTab(next);
    if (next === "resources") setResourcesShown((n) => n + 1);
  }, []);

  /** Shows a source's detail over the document (#473), remembering what opened it. */
  const viewNow = useRef(view);
  viewNow.current = view;
  const detailOpen = useRef(false);
  const show = useCallback((resource: OpenResource, element: HTMLElement | null) => {
    trigger.current = element;
    // Below 900 px the detail takes the document view's place; closing it goes back to the view
    // it was opened from (not to the document when it was opened again from the document itself).
    if (!(detailOpen.current && viewNow.current === "document")) returnView.current = viewNow.current;
    detailOpen.current = true;
    setOpen(resource);
    setView("document");
  }, []);

  const openSource = useCallback(
    (label: string, element: HTMLElement, definition?: string) =>
      show({ label, definition: definition ?? tree?.footnotes.find((f) => f.label === label)?.text }, element),
    [tree, show],
  );

  const closeSource = useCallback(() => {
    detailOpen.current = false;
    setOpen(null);
    setView(returnView.current);
    refocus.current = trigger.current;
    trigger.current = null;
  }, []);

  // After the render that closed the detail (and showed the opener's view again), the focus goes
  // back to what opened it, without scrolling the page.
  useEffect(() => {
    if (open !== null) return;
    const element = refocus.current;
    refocus.current = null;
    if (element?.isConnected) element.focus({ preventScroll: true });
  }, [open]);

  const preset = useMemo(() => ({ subjectId, topicId }), [subjectId, topicId]);
  const base = topicPath(subjectId, topicId);

  return (
    <WorkspaceContext.Provider value={state}>
      <SourceSelectionContext.Provider value={selection}>
        <main className="workspace" data-view={view} data-detail={open === null ? undefined : "open"} aria-label="Espacio de estudio">
          <header className="workspace-header">
            <div className="workspace-bar">
              <ModeSwitch subjectId={subjectId} topicId={topicId} current="build" />
              <h1 className="workspace-title">
                <a href={base} title="Abrir la página del tema">
                  {topicName}
                </a>
              </h1>
              {/* Right-aligned; room is left here for the settings and the profile. */}
              <nav className="workspace-bar-end" aria-label="Más">
                {notes.kind === "ready" && (
                  <a
                    className="workspace-versions"
                    href={`${base}/versions`}
                    aria-label={notes.version === null ? "Versiones" : `Versiones (actual: v${notes.version})`}
                  >
                    Versiones{notes.version === null ? "" : ` · v${notes.version}`}
                  </a>
                )}
                <a className="workspace-home" href="/">
                  Mesa de estudio
                </a>
              </nav>
            </div>
            <div className="workspace-switch" role="group" aria-label="Qué mostrar">
              {VIEWS.map(([key, label]) => (
                <button key={key} type="button" aria-pressed={view === key} onClick={() => setView(key)}>
                  {label}
                </button>
              ))}
            </div>
          </header>
          <div className="workspace-columns">
            <div className="workspace-left">
              <section className="workspace-sources" aria-label="Captura y recursos">
                <WorkspaceTabs<Tab>
                  id="workspace"
                  label="Captura o recursos"
                  active={tab}
                  onChange={changeTab}
                  tabs={[
                    {
                      key: "capture",
                      label: (
                        <>
                          Captura
                          {/* #470: an icon, not «en curso»; its name keeps the state for a screen reader. */}
                          {capturing && (
                            <>
                              {" "}
                              <span
                                className={`workspace-rec${tab === "capture" && recording ? " workspace-rec-on" : ""}`}
                                role="img"
                                aria-label={tab === "capture" ? "en curso" : "en pausa"}
                                title={tab === "capture" ? (recording ? "Grabando" : "Sin grabar") : "En pausa"}
                              />
                            </>
                          )}
                        </>
                      ),
                      className: "workspace-tabpanel-capture",
                      panel: (
                        <CapturePage
                          preset={preset}
                          onRunningChange={setCapturing}
                          onRecordingChange={setRecording}
                          suspended={tab !== "capture"}
                        />
                      ),
                    },
                    {
                      key: "resources",
                      label: "Recursos",
                      panel: (
                        <ResourcesTab
                          subjectId={subjectId}
                          topicId={topicId}
                          tree={tree}
                          refreshKey={resourcesShown}
                          onOpen={show}
                        />
                      ),
                    },
                  ]}
                />
              </section>
              <section className="workspace-chat" aria-label="Chat">
                {/* A capture paused on Recursos (#450) hears nothing: the chat offers its own microphone. */}
                <WorkspaceChatSlot onOpenSource={openSource} capturing={capturing && tab === "capture"} />
              </section>
            </div>
            <section className="workspace-document" aria-label="Documento">
              <DocumentPanel topicName={topicName} tree={tree} onOpenSource={openSource} activeLabel={open?.label ?? null} />
            </section>
            {/* #473: the source's detail, over the document column (its own grid cell, so the
                document underneath is neither remounted nor scrolled). */}
            {open !== null && (
              <div className="workspace-detail">
                <SourcePanel
                  subjectId={subjectId}
                  topicId={topicId}
                  label={open.label}
                  definition={open.definition}
                  onClose={closeSource}
                  variant="overlay"
                />
              </div>
            )}
          </div>
        </main>
      </SourceSelectionContext.Provider>
    </WorkspaceContext.Provider>
  );
}
