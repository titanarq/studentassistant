import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CapturePage from "../capture/CapturePage";
import { fetchTopics, topicPath } from "../desk/api";
import { parseNotes } from "../notes/markdown";
import { fetchPending } from "../pending/api";
import DocumentPanel from "./DocumentPanel";
import ResourcesTab, { type OpenResource } from "./ResourcesTab";
import { useWorkspaceState, WorkspaceContext } from "./state";
import WorkspaceChatSlot from "./WorkspaceChatSlot";
import WorkspaceTabs from "./WorkspaceTabs";
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
 * **Recursos** (the topic's sources and the sources viewer) above the chat, and on the right the
 * topic's document (`apuntes.md`), which the student can also edit (`DocumentPanel`, #316). A
 * provenance footnote opens its source in **Recursos**. The capture tab stays mounted while hidden, so a running session goes on.
 * Below 900 px the columns become one, with the switch Documento | Captura/Recursos | Chat.
 */
export default function WorkspacePage({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const state = useWorkspaceState(subjectId, topicId);
  const { notes } = state;
  const [topicName, setTopicName] = useState(topicId);
  const [tab, setTab] = useState<Tab>("capture");
  const [view, setView] = useState<NarrowView>("document");
  const [capturing, setCapturing] = useState(false);
  const [open, setOpen] = useState<OpenResource | null>(null);
  const [resourcesShown, setResourcesShown] = useState(0);
  const [pending, setPending] = useState<number | null>(null);
  const trigger = useRef<HTMLElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchTopics(subjectId).then((result) => {
      const topic = result.kind === "ok" ? result.value.find((t) => t.topic_id === topicId) : undefined;
      if (!cancelled && topic) setTopicName(topic.name);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

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
  }, [subjectId, topicId, notesKey]);

  const tree = useMemo(() => (notes.kind === "ready" ? parseNotes(notes.text) : null), [notes]);

  const changeTab = useCallback((next: Tab) => {
    setTab(next);
    if (next === "resources") setResourcesShown((n) => n + 1);
  }, []);

  const openSource = useCallback(
    (label: string, element: HTMLElement) => {
      trigger.current = element;
      setOpen({ label, definition: tree?.footnotes.find((f) => f.label === label)?.text });
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
      <div className="workspace" data-view={view}>
        <header className="workspace-header">
          <p className="crumbs">
            <a href={base}>← Tema {topicName}</a>
            <a className="crumbs-home" href="/">
              Mesa de estudio
            </a>
          </p>
          <h1>Espacio de estudio</h1>
          <p className="page-context">Tema {topicName}</p>
          <p className="workspace-pending" role="status" aria-label="Dudas pendientes">
            {pending === null ? null : (
              <a href={`${base}/pending`}>
                {pending === 1 ? "1 duda pendiente" : `${pending} dudas pendientes`}
              </a>
            )}
          </p>
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
              <WorkspaceChatSlot onOpenSource={openSource} />
            </section>
          </div>
          <section className="workspace-document" aria-label="Documento">
            <DocumentPanel topicName={topicName} tree={tree} onOpenSource={openSource} activeLabel={open?.label ?? null} />
          </section>
        </div>
      </div>
    </WorkspaceContext.Provider>
  );
}
