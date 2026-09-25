import { useCallback, useEffect, useRef, useState } from "react";
import { describeFailure } from "../desk/api";
import { describeActionFailure } from "../pending/doubts";
import {
  askWhy,
  type ChatHistory,
  type ChatRef,
  describeChatFailure,
  fetchChatHistory,
  sendChatMessage,
  undoLastTurn,
  type WhyAnchor,
} from "./api";

/** One exchange of the conversation as the chat shows it. */
export interface ChatEntry {
  key: string;
  /** `explain` for a "¿Por qué pusiste esto?" answer. */
  kind: "revise" | "explain";
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
  /** The sources a "¿Por qué?" answer points to. */
  refs: ChatRef[];
}

/** What one turn asks: a chat message, or "¿Por qué pusiste esto?" on a block. */
type TurnRequest = { kind: "revise"; message: string } | { kind: "explain"; message: string; anchor: WhyAnchor };

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
  /** "¿Por qué pusiste esto?" on a block; `message` is how the chat shows the question. */
  ask: (anchor: WhyAnchor, message: string) => void;
  retry: () => void;
  undo: () => void;
}

function fromHistory(history: ChatHistory, diffs: Map<string, string>): ChatEntry[] {
  return history.turns.map((turn, index) => ({
    key: `h${index}-${turn.time}`,
    kind: turn.kind,
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
    refs: turn.refs,
  }));
}

/**
 * The state of the chat with the editor beside the notes: the conversation (read once from
 * `GET .../notes/chat`), one streamed turn at a time (`send`, or `ask` for "¿Por qué pusiste
 * esto?" on a block, `POST .../notes/why`), the undo of the latest applied turn
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
  const [stopped, setStopped] = useState<{ key: string; request: TurnRequest } | null>(null);
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
    async (request: TurnRequest, confirmOverCap: boolean, replace: string | null) => {
      if (running.current) return;
      running.current = true;
      const key = `live${++counter.current}`;
      setBusy("send");
      setNotice(null);
      setStopped(null);
      const fresh: ChatEntry = {
        key,
        kind: request.kind,
        message: request.message,
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
        refs: [],
      };
      setEntries((current) => [...current.filter((entry) => entry.key !== replace), fresh]);
      let attempt = 1;
      const handlers = {
        confirmOverCap,
        onDelta: (text: string, of: number) => {
          if (of < attempt) return;
          attempt = of;
          update(key, (entry) => ({ reply: entry.reply + text }));
        },
        onRestart: (of: number) => {
          attempt = of;
          update(key, () => ({ reply: "" }));
        },
      };
      const finish = () => {
        running.current = false;
        if (!mounted.current) return false;
        setBusy(null);
        return true;
      };
      if (request.kind === "explain") {
        const outcome = await askWhy(subjectId, topicId, request.anchor, handlers);
        if (!finish()) return;
        if (outcome.kind === "ok") {
          const { question, reply, refs, warning } = outcome.value;
          update(key, () => ({ message: question, reply, refs, warning, streaming: false }));
        } else if (outcome.kind === "interrupted") {
          await loadHistory();
        } else {
          update(key, () => ({ streaming: false, failure: describeChatFailure(outcome) }));
          if (outcome.kind === "refused" && outcome.overCap) setStopped({ key, request });
        }
        return;
      }
      const outcome = await sendChatMessage(subjectId, topicId, request.message, handlers);
      if (!finish()) return;
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
      if (outcome.kind === "refused" && outcome.overCap) setStopped({ key, request });
    },
    [subjectId, topicId, loadHistory],
  );

  const send = useCallback(
    (message: string) => {
      const text = message.trim();
      if (text !== "") void run({ kind: "revise", message: text }, false, null);
    },
    [run],
  );

  const ask = useCallback(
    (anchor: WhyAnchor, message: string) => void run({ kind: "explain", message, anchor }, false, null),
    [run],
  );

  const retry = useCallback(() => {
    if (stopped !== null) void run(stopped.request, true, stopped.key);
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
    ask,
    retry,
    undo: () => void undo(),
  };
}
