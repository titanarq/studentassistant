import { type CSSProperties, type ReactNode, useCallback, useRef, useState } from "react";
import { topicPath } from "../desk/api";
import ModeSwitch, { type Mode } from "../study/ModeSwitch";
import ColumnDivider from "./ColumnDivider";
import { panelState, readSidePercent, toggleChat, togglePanel, writeSidePercent } from "./layoutState";
import "./workspace.css";

/** What the single column shows below 900 px: the right card, the left card(s) or the chat. */
export type NarrowView = "document" | "left" | "chat";

export interface WorkspaceFrameProps {
  /** Which of the two modes this is: the header's switch marks it as the current one. */
  mode: Mode;
  subjectId: string;
  topicId: string;
  topicName: string;
  /** The accessible name of the whole screen. */
  label: string;
  /** Extra class names on the root (the mode's own rules hang from them). */
  className?: string;
  /** Links at the right of the header band, before «Mesa de estudio». */
  barEnd?: ReactNode;
  /** The labels of the single-column switch, in the order Documento, left, Chat. */
  views: Record<NarrowView, string>;
  view: NarrowView;
  onView: (view: NarrowView) => void;
  /** The left card (Captura/Recursos, the study options): its accessible name and class. */
  leftLabel: string;
  leftClassName?: string;
  left: ReactNode;
  /** The chat card's content, at the bottom of the left column. */
  chat: ReactNode;
  /** The right card: the document (or the study document with its material panel). */
  documentClassName?: string;
  /** `data-panel` of the right card (a study material or source shown over its edge). */
  documentPanel?: boolean;
  document: ReactNode;
  /** The detail over the right card's cell (#473): the document underneath stays mounted. */
  detail?: ReactNode;
  /**
   * Whether the left card is collapsed to its header (#534), when the host owns that state
   * (Construir: its default depends on the notes). Left out, the frame keeps it itself, expanded.
   */
  leftCollapsed?: boolean;
  onLeftCollapsed?: (collapsed: boolean) => void;
}

const ORDER: NarrowView[] = ["document", "left", "chat"];

/** A vertical double chevron (a `»` turned up or down): the toggles of the chat and the left card. */
function Chevrons({ direction }: { direction: "up" | "down" }) {
  return (
    <svg
      className="workspace-chevrons"
      data-direction={direction}
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {direction === "up" ? (
        <>
          <path d="M3.5 7.5 8 3l4.5 4.5" />
          <path d="M3.5 13 8 8.5 12.5 13" />
        </>
      ) : (
        <>
          <path d="M3.5 3 8 7.5 12.5 3" />
          <path d="M3.5 8.5 8 13l4.5-4.5" />
        </>
      )}
    </svg>
  );
}

/**
 * The frame shared by **Construir** and **Estudiar** (#487): the desk under the header band (the
 * switch Construir · Estudiar, the topic's name, the links on the right), and two columns -- on the
 * left a card (`leftLabel`) above the chat card, on the right the document card, with a fluid split
 * -- that fill one viewport from 900 px on (#450, #458: only the panels scroll inside, the chat's
 * input always on screen). Below 900 px the columns become one, with the switch Documento | left |
 * Chat (`views`). The styles are `workspace.css`: both modes use the same classes, so every change
 * of the frame applies to both. `detail` is the #473 overlay over the document's grid cell.
 */
export default function WorkspaceFrame({
  mode,
  subjectId,
  topicId,
  topicName,
  label,
  className,
  barEnd,
  views,
  view,
  onView,
  leftLabel,
  leftClassName,
  left,
  chat,
  documentClassName,
  documentPanel = false,
  document,
  detail,
  leftCollapsed,
  onLeftCollapsed,
}: WorkspaceFrameProps) {
  const base = topicPath(subjectId, topicId);
  // #534: the chat column's width (shared by both modes, kept in sessionStorage), the chat
  // expanded over the left card, and the left card collapsed to its header.
  const columns = useRef<HTMLDivElement | null>(null);
  const [side, setSide] = useState(readSidePercent);
  const [chatExpanded, setChatExpanded] = useState(false);
  const [ownCollapsed, setOwnCollapsed] = useState(false);
  const collapsed = leftCollapsed ?? ownCollapsed;
  const setCollapsed = useCallback(
    (next: boolean) => {
      setOwnCollapsed(next);
      onLeftCollapsed?.(next);
    },
    [onLeftCollapsed],
  );
  const layout = { panelCollapsed: collapsed, chatExpanded };
  const panel = panelState(layout);
  const leftName = leftLabel.toLowerCase();
  const detailShown = detail !== undefined && detail !== null && detail !== false;
  return (
    <main
      className={className === undefined ? "workspace" : `workspace ${className}`}
      data-mode={mode}
      data-view={view}
      data-detail={detailShown ? "open" : undefined}
      data-chat={chatExpanded ? "expanded" : undefined}
      aria-label={label}
    >
      <header className="workspace-header">
        <div className="workspace-bar">
          <ModeSwitch subjectId={subjectId} topicId={topicId} current={mode} />
          <h1 className="workspace-title">
            <a href={base} title="Abrir la página del tema">
              {topicName}
            </a>
          </h1>
          {/* Right-aligned; room is left here for the settings and the profile. */}
          <nav className="workspace-bar-end" aria-label="Más">
            {barEnd}
            <a className="workspace-home" href="/">
              Mesa de estudio
            </a>
          </nav>
        </div>
        <div className="workspace-switch" role="group" aria-label="Qué mostrar">
          {ORDER.map((key) => (
            <button key={key} type="button" aria-pressed={view === key} onClick={() => onView(key)}>
              {views[key]}
            </button>
          ))}
        </div>
      </header>
      <div className="workspace-columns" ref={columns} style={{ "--workspace-side": `${side}%` } as CSSProperties}>
        <div className="workspace-left">
          <section
            id="workspace-left-card"
            className={leftClassName === undefined ? "workspace-sources" : `workspace-sources ${leftClassName}`}
            aria-label={leftLabel}
            data-collapsed={panel === "collapsed" ? "true" : undefined}
          >
            {/* #538: floats at the card's top-right corner (the header row's right end when collapsed). */}
            {panel !== "hidden" && (
              <button
                type="button"
                className="workspace-toggle workspace-left-toggle"
                aria-expanded={!collapsed}
                aria-controls="workspace-left-card"
                aria-label={collapsed ? `Mostrar ${leftName}` : `Ocultar ${leftName}`}
                title={collapsed ? `Mostrar ${leftName}` : `Ocultar ${leftName}`}
                onClick={() => setCollapsed(togglePanel(layout).panelCollapsed)}
              >
                <Chevrons direction={collapsed ? "down" : "up"} />
              </button>
            )}
            {left}
          </section>
          <section className="workspace-chat" aria-label="Chat">
            {/* #538: floats at the card's top-right corner; shown on the two-column layout only
                (the one-column switch picks a view). */}
            <button
              type="button"
              className="workspace-toggle workspace-chat-toggle"
              aria-expanded={chatExpanded}
              aria-label={chatExpanded ? "Reducir chat" : "Ampliar chat"}
              title={chatExpanded ? "Reducir chat" : "Ampliar chat"}
              onClick={() => setChatExpanded(toggleChat(layout).chatExpanded)}
            >
              <Chevrons direction={chatExpanded ? "down" : "up"} />
            </button>
            {chat}
          </section>
        </div>
        <ColumnDivider value={side} columns={columns} onChange={setSide} onCommit={writeSidePercent} />
        <section
          className={documentClassName === undefined ? "workspace-document" : `workspace-document ${documentClassName}`}
          aria-label="Documento"
          data-panel={documentPanel ? "open" : undefined}
        >
          {document}
        </section>
        {/* #473: the source's detail, over the document column (its own grid cell, so the
            document underneath is neither remounted nor scrolled). */}
        {detailShown && <div className="workspace-detail">{detail}</div>}
      </div>
    </main>
  );
}
