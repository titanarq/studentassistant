import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { undoLastTurn } from "../../chat/api";
import { describeFailure } from "../../desk/api";
import { describeActionFailure } from "../../pending/doubts";
import { confirmOverCap, fetchWorkspaceHistory, postMessage, type WorkspaceEvent } from "./api";
import { connectWorkspaceStream } from "./stream";
import { canRetry, type ChatEntry, INITIAL_CHAT, reduceChat } from "./turns";

export type Connection = "connecting" | "live" | "down";

export interface WorkspaceChat {
  entries: ChatEntry[];
  canUndo: boolean;
  /** What this page is running: a typed message being posted (or a confirmation), an undo, or nothing. */
  busy: "send" | "undo" | null;
  /** A doubt asked in the chat waits for its answer. */
  doubtAsked: boolean;
  connection: Connection;
  historyFailure: string | null;
  /** The last line about an undo, in Spanish. */
  notice: string | null;
  send: (message: string) => void;
  /** "Continuar igualmente" on a turn stopped at the cost cap. */
  retry: (key: string) => void;
  undo: () => void;
}

export interface WorkspaceChatOptions {
  subjectId: string;
  topicId: string;
  /** The workspace state's notes reload (`state.ts`); `sections` are highlighted when given. */
  reloadNotes: (changedSections?: string[]) => void;
  /** Backoff of the stream's reconnects (tests pass short ones). */
  retryDelays?: readonly number[];
  /** A doubt was asked or resolved: the page's pending-doubts counter is read again. */
  onDoubtsChanged?: () => void;
}

const DOUBT_EVENTS = new Set<WorkspaceEvent["type"]>(["doubt.asked", "doubt.resolved", "doubts.auto_resolved"]);

/**
 * The state of the workspace chat panel (#317, #329): the history, the live workspace stream
 * (every reconnect re-reads the history and the notes, so nothing that happened meanwhile is
 * missed or shown twice), typed messages (to the request classifier, `POST .../workspace/messages`),
 * the undo of the latest applied turn and "Continuar igualmente" (the stopped request confirmed
 * past the cost cap through the same route, #351). Every `notes.changed` of the
 * stream reloads the document, with the sections the turn touched highlighted when the turn is
 * known; every doubt asked or resolved calls `onDoubtsChanged`.
 */
export function useWorkspaceChat({ subjectId, topicId, reloadNotes, retryDelays, onDoubtsChanged }: WorkspaceChatOptions): WorkspaceChat {
  const [state, dispatch] = useReducer(reduceChat, INITIAL_CHAT);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [historyFailure, setHistoryFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState<"send" | "undo" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const running = useRef(false);
  const counter = useRef(0);
  const mounted = useRef(true);
  const entries = useRef<ChatEntry[]>([]);
  entries.current = state.entries;
  const delays = useRef(retryDelays);
  const reload = useRef(reloadNotes);
  reload.current = reloadNotes;
  const doubtsChanged = useRef(onDoubtsChanged);
  doubtsChanged.current = onDoubtsChanged;
  /** The sections each turn changed, known as soon as its result arrives (before a re-render). */
  const sections = useRef(new Map<string, string[]>());

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const loadHistory = useCallback(async () => {
    const result = await fetchWorkspaceHistory(subjectId, topicId);
    if (!mounted.current) return;
    if (result.kind === "ok") {
      dispatch({ type: "history", history: result.value });
      setHistoryFailure(null);
    } else {
      setHistoryFailure(`No se pudo leer la conversación con el asistente: ${describeFailure(result)}`);
    }
  }, [subjectId, topicId]);

  useEffect(() => {
    let loaded = false;
    const onEvent = (event: WorkspaceEvent) => {
      if (event.type === "turn.result" && event.outcome.notesChanged) {
        sections.current.set(event.turnId, event.outcome.changedSections);
      }
      dispatch({ type: "event", event });
      if (DOUBT_EVENTS.has(event.type)) doubtsChanged.current?.();
      if (event.type === "notes.changed") {
        const changed = event.turnId !== null ? sections.current.get(event.turnId) : undefined;
        reload.current(changed ?? []);
      }
    };
    const close = connectWorkspaceStream(subjectId, topicId, {
      retryDelays: delays.current,
      onEvent,
      onOpen: (reconnected) => {
        setConnection("live");
        loaded = true;
        void loadHistory();
        if (reconnected) reload.current();
      },
      onDown: () => {
        setConnection("down");
        // Without a stream the conversation so far is still shown.
        if (!loaded) {
          loaded = true;
          void loadHistory();
        }
      },
    });
    return close;
  }, [subjectId, topicId, loadHistory]);

  const send = useCallback(
    (message: string) => {
      const text = message.trim();
      if (text === "" || running.current) return;
      running.current = true;
      const key = `post${++counter.current}`;
      setBusy("send");
      setNotice(null);
      dispatch({ type: "post.start", key, message: text, time: new Date().toISOString() });
      void postMessage(subjectId, topicId, text).then((result) => {
        running.current = false;
        if (!mounted.current) return;
        setBusy(null);
        dispatch({ type: "post.done", key, result });
      });
    },
    [subjectId, topicId],
  );

  const retry = useCallback(
    (key: string) => {
      const entry = entries.current.find((e) => e.key === key);
      if (entry === undefined || !canRetry(entry) || entry.turnId === null || running.current) return;
      const turnId = entry.turnId;
      running.current = true;
      setBusy("send");
      setNotice(null);
      dispatch({ type: "confirm.start", key });
      void confirmOverCap(subjectId, topicId, turnId).then((result) => {
        running.current = false;
        if (!mounted.current) return;
        setBusy(null);
        dispatch({ type: "confirm.done", key, result });
      });
    },
    [subjectId, topicId],
  );

  const undo = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    setBusy("undo");
    setNotice(null);
    const result = await undoLastTurn(subjectId, topicId);
    running.current = false;
    if (!mounted.current) return;
    setBusy(null);
    if (result.kind !== "ok") {
      setNotice(`No se pudo deshacer: ${describeActionFailure(result)}`);
      return;
    }
    const { summary, notes_changed, undone_commit } = result.value;
    setNotice(summary !== null ? `Se ha deshecho el cambio «${summary}».` : "Se ha deshecho el último cambio.");
    dispatch({ type: "undone", commit: undone_commit });
    if (notes_changed) reload.current([]);
    await loadHistory();
  }, [subjectId, topicId, loadHistory]);

  return {
    entries: state.entries,
    canUndo: state.canUndo,
    busy,
    doubtAsked: state.entries.some((e) => e.kind === "doubt" && e.doubt?.status === "open"),
    connection,
    historyFailure,
    notice,
    send,
    retry,
    undo: () => void undo(),
  };
}
