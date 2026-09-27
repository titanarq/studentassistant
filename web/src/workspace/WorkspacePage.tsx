import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CapturePage from "../capture/CapturePage";
import { fetchTopics, topicPath } from "../desk/api";
import { parseNotes } from "../notes/markdown";
import { fetchPending } from "../pending/api";
import DocumentPanel from "./DocumentPanel";
import ResourcesTab, { type OpenResource } from "./ResourcesTab";
import { useWorkspaceState, WorkspaceContext } from "./state";
import WorkspaceChatSlot from "./WorkspaceChatSlot";
import WorkspaceCost from "./WorkspaceCost";
import WorkspaceTabs from "./WorkspaceTabs";
import ModeSwitch from "../study/ModeSwitch";
import "../notes/notes.css";
import "./workspace.css";

type Tab = "capture" | "resources";
/** What the single column shows below 900 px. */
export type NarrowView = "document" | "left" | "chat";

export { EMPTY_NOTES } from "./DocumentPanel";

/** The doubts counter's tooltip (#413): where the doubts are answered. */
export const DOUBTS_TOOLTIP = "Las dudas te las pregunta el chat de este espacio; contéstalas allí.";

const VIEWS: Array<[NarrowView, string]> = [
  ["document", "Documento"],
  ["left", "Captura/Recursos"],
  ["chat", "Chat"],
];

/**
 * `/subjects/<subject>/topics/<topic>/workspace`, "Espacio de estudio" (#312, epic #311): one
 * screen with, on the left, the tabs **Captura** (the capture flow, preset to this topic) and
 * **Recursos** (the topic's sources and the sources viewer) above the chat, and on the right the
 * topic's document (`apuntes.md`), which the student can also edit (`DocumentPanel`, #316). A
 * provenance footnote opens its source in **Recursos**. The capture tab stays mounted while hidden, so a running session goes on.
 * The header also carries the doubts counter (plain text since #413: the doubts are asked in the
 * chat, never on the legacy `/pending` page, epic #311), a **Versiones** link to the notes' history and the
 * spend of the open session or of the topic (`WorkspaceCost`, #372).
 * Below 900 px the columns become one, with the switch Documento | Captura/Recursos | Chat.
 */
export default function WorkspacePage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const state = useWorkspaceState(subjectId, topicId);
  const { notes } = state;
  const [topicName, setTopicName] = useState(topicId);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("capture");
  const [view, setView] = useState<NarrowView>("document");
  const [capturing, setCapturing] = useState(false);
  const [open, setOpen] = useState<OpenResource | null>(null);
  const [resourcesShown, setResourcesShown] = useState(0);
  const [pending, setPending] = useState<number | null>(null);
  const trigger = useRef<HTMLElement | null>(null);

  // Read again when a capture starts or ends here, for the open session the cost line follows.
  useEffect(() => {
    let cancelled = false;
    void fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (cancelled || !topic) return;
      setTopicName(topic.name);
      setSessionId(topic.open_session_id ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, capturing]);

  // The doubts counter is read again whenever the document changed.
  const notesKey = notes.kind === "ready" ? `${notes.revision ?? ""}:${notes.text.length}:${notes.version}` : notes.kind;
  useEffect(() => {
    let cancelled = false;
    void fetchPending(subjectId, topicId, "open").then((result) => {
      if (!cancelled && result.kind === "ok") setPending(result.value.open_count);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, notesKey, state.doubtsKey]);

  const tree = useMemo(() => (notes.kind === "ready" ? parseNotes(notes.text) : null), [notes]);

  const changeTab = useCallback((next: Tab) => {
    setTab(next);
    if (next === "resources") setResourcesShown((n) => n + 1);
  }, []);

  const openSource = useCallback(
    (label: string, element: HTMLElement, definition?: string) => {
      trigger.current = element;
      setOpen({ label, definition: definition ?? tree?.footnotes.find((f) => f.label === label)?.text });
      changeTab("resources");
      setView("left");
    },
    [tree, changeTab],
  );

  const closeSource = useCallback(() => {
    setOpen(null);
    const element = trigger.current;
    trigger.current = null;
    if (element?.isConnected) element.focus({ preventScroll: true });
  }, []);

  const openResource = useCallback((resource: OpenResource) => {
    trigger.current = null;
    setOpen(resource);
  }, []);

  const preset = useMemo(() => ({ subjectId, topicId }), [subjectId, topicId]);
  const base = topicPath(subjectId, topicId);

  return (
    <WorkspaceContext.Provider value={state}>
      <main className="workspace" data-view={view}>
        <header className="workspace-header">
          <p className="crumbs">
            <a href={base}>← Tema {topicName}</a>
            <a className="crumbs-home" href="/">
              Mesa de estudio
            </a>
          </p>
          <h1>Espacio de estudio</h1>
          <ModeSwitch subjectId={subjectId} topicId={topicId} current="build" />
          <p className="page-context">Tema {topicName}</p>
          <div className="workspace-meta">
            <p
              className="workspace-pending"
              role="status"
              aria-label="Dudas pendientes"
              title={pending === null ? undefined : DOUBTS_TOOLTIP}
            >
              {pending === null ? null : pending === 1 ? "1 duda pendiente" : `${pending} dudas pendientes`}
            </p>
            {notes.kind === "ready" && (
              <p className="workspace-versions">
                <a
                  href={`${base}/versions`}
                  aria-label={notes.version === null ? "Versiones" : `Versiones (actual: v${notes.version})`}
                >
                  Versiones
                </a>
              </p>
            )}
            <WorkspaceCost subjectId={subjectId} topicId={topicId} sessionId={sessionId} refreshKey={notesKey} />
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
                        {capturing && (
                          <>
                            {" "}
                            <span className="workspace-live">en curso</span>
                          </>
                        )}
                      </>
                    ),
                    panel: <CapturePage preset={preset} onRunningChange={setCapturing} />,
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
                        open={open}
                        onOpen={openResource}
                        onClose={closeSource}
                      />
                    ),
                  },
                ]}
              />
            </section>
            <section className="workspace-chat" aria-label="Chat">
              <WorkspaceChatSlot onOpenSource={openSource} capturing={capturing} />
            </section>
          </div>
          <section className="workspace-document" aria-label="Documento">
            <DocumentPanel topicName={topicName} tree={tree} onOpenSource={openSource} activeLabel={open?.label ?? null} />
          </section>
        </div>
      </main>
    </WorkspaceContext.Provider>
  );
}
