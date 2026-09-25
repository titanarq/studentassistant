import { useCallback, useEffect, useRef, useState } from "react";
import { describeFailure } from "../desk/api";
import { describeActionFailure } from "../pending/doubts";
import {
  type ChatHistory,
  type ChatOutcome,
  describeChatFailure,
  fetchChatHistory,
  sendChatMessage,
  undoLastTurn,
} from "./api";

/** One exchange of the conversation as the chat shows it. */
export interface ChatEntry {
  key: string;
  message: string;
  /** The reply streamed so far, or the final one. */
  reply: string;
  /** The reply is still being written. */
  streaming: boolean;
  applied: boolean;
  summary: string | null;
  changedSections: string[];
  /** The unified diff of the change; `null` for turns read from the history (it has no diffs). */
  diff: string | null;
  commit: string | null;
  undone: boolean;
  warning: string | null;
  /** Why the turn failed, in Spanish. */
  failure: string | null;
}

export interface EditorChat {
  entries: ChatEntry[];
  canUndo: boolean;
  /** What is running: a chat turn, an undo, or nothing. */
  busy: "send" | "undo" | null;
  /** The conversation so far could not be read. */
  historyFailure: string | null;
  /** The last line about an undo, in Spanish. */
  notice: string | null;
  /** The last turn stopped at a reached cost cap: `retry` repeats it with `confirm_over_cap`. */
  overCap: boolean;
  send: (message: string) => void;
  retry: () => void;
  undo: () => void;
}

function fromHistory(history: ChatHistory, diffs: Map<string, string>): ChatEntry[] {
  return history.turns.map((turn, index) => ({
    key: `h${index}-${turn.time}`,
    message: turn.message,
    reply: turn.reply,
    streaming: false,
    applied: turn.applied,
    summary: turn.summary,
    changedSections: turn.changed_sections,
    diff: turn.commit !== null ? (diffs.get(turn.commit) ?? null) : null,
    commit: turn.commit,
    undone: turn.undone,
    warning: turn.warning,
    failure: null,
  }));
}

/**
 * The state of the chat with the editor beside the notes: the conversation (read once from
 * `GET .../notes/chat`), one streamed turn at a time (`send`), the undo of the latest applied turn
 * (`undo`) and the "Continuar igualmente" of a reached cost cap (`retry`). `onNotesChanged` runs
 * after a turn or an undo changed the notes, with the anchors of the sections the turn touched
 * (none for an undo), so the page can read the notes again and highlight them.
 */
export function useEditorChat(
  subjectId: string,
  topicId: string,
  onNotesChanged: (changedSections: string[]) => void,
): EditorChat {
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [canUndo, setCanUndo] = useState(false);
  const [busy, setBusy] = useState<"send" | "undo" | null>(null);
  const [historyFailure, setHistoryFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stopped, setStopped] = useState<{ key: string; message: string } | null>(null);
  const running = useRef(false);
  const counter = useRef(0);
  const diffs = useRef(new Map<string, string>());
  const changed = useRef(onNotesChanged);
  changed.current = onNotesChanged;
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const loadHistory = useCallback(async () => {
    const result = await fetchChatHistory(subjectId, topicId);
    if (!mounted.current) return;
    if (result.kind === "ok") {
      setEntries(fromHistory(result.value, diffs.current));
      setCanUndo(result.value.can_undo);
      setHistoryFailure(null);
    } else {
      setHistoryFailure(`No se pudo leer la conversación con el editor: ${describeFailure(result)}`);
    }
  }, [subjectId, topicId]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const update = (key: string, change: (entry: ChatEntry) => Partial<ChatEntry>) =>
    setEntries((current) => current.map((entry) => (entry.key === key ? { ...entry, ...change(entry) } : entry)));

  const run = useCallback(
    async (message: string, confirmOverCap: boolean, replace: string | null) => {
      if (running.current) return;
      running.current = true;
      const key = `live${++counter.current}`;
      setBusy("send");
      setNotice(null);
      setStopped(null);
      const fresh: ChatEntry = {
        key,
        message,
        reply: "",
        streaming: true,
        applied: false,
        summary: null,
        changedSections: [],
        diff: null,
        commit: null,
        undone: false,
        warning: null,
        failure: null,
      };
      setEntries((current) => [...current.filter((entry) => entry.key !== replace), fresh]);
      let attempt = 1;
      const outcome: ChatOutcome = await sendChatMessage(subjectId, topicId, message, {
        confirmOverCap,
        onDelta: (text, of) => {
          if (of < attempt) return;
          attempt = of;
          update(key, (entry) => ({ reply: entry.reply + text }));
        },
        onRestart: (of) => {
          attempt = of;
          update(key, () => ({ reply: "" }));
        },
      });
      running.current = false;
      if (!mounted.current) return;
      setBusy(null);
      if (outcome.kind === "ok") {
        const value = outcome.value;
        if (value.commit !== null && value.diff !== "") diffs.current.set(value.commit, value.diff);
        update(key, () => ({
          reply: value.reply,
          streaming: false,
          applied: value.applied,
          summary: value.summary,
          changedSections: value.changed_sections,
          diff: value.diff !== "" ? value.diff : null,
          commit: value.commit,
          warning: value.warning,
        }));
        if (value.applied && value.commit !== null) setCanUndo(true);
        if (value.notes_changed) changed.current(value.changed_sections);
        return;
      }
      if (outcome.kind === "interrupted") {
        // The turn runs on in the backend: what it saved shows up in the history and the notes.
        await loadHistory();
        changed.current([]);
        return;
      }
      update(key, () => ({ streaming: false, failure: describeChatFailure(outcome) }));
      if (outcome.kind === "refused" && outcome.overCap) setStopped({ key, message });
    },
    [subjectId, topicId, loadHistory],
  );

  const send = useCallback(
    (message: string) => {
      const text = message.trim();
      if (text !== "") void run(text, false, null);
    },
    [run],
  );

  const retry = useCallback(() => {
    if (stopped !== null) void run(stopped.message, true, stopped.key);
  }, [run, stopped]);

  const undo = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    setBusy("undo");
    setNotice(null);
    setStopped(null);
    const result = await undoLastTurn(subjectId, topicId);
    running.current = false;
    if (!mounted.current) return;
    setBusy(null);
    if (result.kind !== "ok") {
      setNotice(`No se pudo deshacer: ${describeActionFailure(result)}`);
      return;
    }
    const { summary, notes_changed } = result.value;
    setNotice(summary !== null ? `Se ha deshecho el cambio «${summary}».` : "Se ha deshecho el último cambio.");
    setEntries((current) =>
      current.map((entry) => (entry.commit === result.value.undone_commit ? { ...entry, undone: true } : entry)),
    );
    if (notes_changed) changed.current([]);
    await loadHistory();
  }, [subjectId, topicId, loadHistory]);

  return {
    entries,
    canUndo,
    busy,
    historyFailure,
    notice,
    overCap: stopped !== null,
    send,
    retry,
    undo: () => void undo(),
  };
}
