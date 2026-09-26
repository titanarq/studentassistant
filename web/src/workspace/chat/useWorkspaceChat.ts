import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { undoLastTurn } from "../../chat/api";
import { describeFailure } from "../../desk/api";
import { describeActionFailure } from "../../pending/doubts";
import { confirmPrepareNotes, fetchWorkspaceHistory, type SendOutcome, sendTyped, type WorkspaceEvent } from "./api";
import { connectWorkspaceStream } from "./stream";
import { type ChatEntry, INITIAL_CHAT, reduceChat } from "./turns";

export type Connection = "connecting" | "live" | "down";

export interface WorkspaceChat {
  entries: ChatEntry[];
  canUndo: boolean;
  /** What this page is running: a typed message (or a retry), an undo, or nothing. */
  busy: "send" | "undo" | null;
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
}

/**
 * The state of the workspace chat panel (#317): the history, the live workspace stream (every
 * reconnect re-reads the history and the notes, so nothing that happened meanwhile is missed or
 * shown twice), typed messages, the undo of the latest applied turn and "Continuar igualmente".
 * Every `notes.changed` of the stream reloads the document, with the sections the turn touched
 * highlighted when the turn is known.
 */
export function useWorkspaceChat({ subjectId, topicId, reloadNotes, retryDelays }: WorkspaceChatOptions): WorkspaceChat {
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

  const run = useCallback(
    async (key: string, work: () => Promise<SendOutcome>) => {
      const result = await work();
      running.current = false;
      if (!mounted.current) return;
      setBusy(null);
      dispatch({ type: "local.done", key, result });
      if (result.kind === "ok") {
        if (result.outcome.notesChanged) reload.current(result.outcome.changedSections);
      } else if (result.kind === "interrupted") {
        // The turn runs on in the backend: what it saved shows up in the history and the notes.
        await loadHistory();
        reload.current([]);
      }
    },
    [loadHistory],
  );

  const start = useCallback(
    (message: string, replace: string | undefined, kind?: string): string | null => {
      if (running.current) return null;
      running.current = true;
      const key = replace ?? `local${++counter.current}`;
      setBusy("send");
      setNotice(null);
      dispatch({ type: "local.start", key, message, kind, replace, time: new Date().toISOString() });
      return key;
    },
    [],
  );

  const sendMessage = useCallback(
    (message: string, replace?: string, confirmOverCap = false) => {
      const key = start(message, replace);
      if (key === null) return;
      void run(key, () =>
        sendTyped(subjectId, topicId, message, {
          confirmOverCap,
          onDelta: (text, attempt) => dispatch({ type: "local.delta", key, text, attempt }),
          onRestart: (attempt) => dispatch({ type: "local.restart", key, attempt }),
        }),
      );
    },
    [subjectId, topicId, start, run],
  );

  const send = useCallback(
    (message: string) => {
      const text = message.trim();
      if (text !== "") sendMessage(text);
    },
    [sendMessage],
  );

  const retry = useCallback(
    (key: string) => {
      const entry = entries.current.find((e) => e.key === key);
      if (entry === undefined || !entry.overCap) return;
      if (entry.kind === "prepare_notes") {
        const started = start(entry.message ?? "", key, "prepare_notes");
        if (started !== null) void run(started, () => confirmPrepareNotes(subjectId, topicId));
        return;
      }
      const message = entry.message ?? entry.transcript?.text ?? "";
      if (message.trim() !== "") sendMessage(message, key, true);
    },
    [subjectId, topicId, start, run, sendMessage],
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
    connection,
    historyFailure,
    notice,
    send,
    retry,
    undo: () => void undo(),
  };
}
