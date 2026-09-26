/**
 * The workspace chat's list of turns (#317) as a pure reducer, so the merge rules are in one
 * place: the history (`GET .../notes/chat`), the live events of the workspace stream and the
 * typed turns this page sends (whose own `POST .../notes/chat` stream answers them too).
 *
 * - A turn is keyed by its `turn_id`. A spoken request shows as soon as it is detected
 *   (`request.detected`, "En cola…", by `request_id`, in arrival order) and becomes its turn on
 *   `turn.started`.
 * - A typed turn of this page starts as a local entry fed by its own POST stream; the typed
 *   `turn.started` that the workspace stream broadcasts meanwhile is adopted by it (turns of a topic
 *   run one at a time) and from then on only the workspace stream writes its reply, so no text is
 *   shown twice. The POST's `result` carries the real `turn_id`: a wrongly adopted turn (another
 *   tab's, which held the notes first) is split off into its own entry again.
 * - A history read replaces every entry it has (by `turn_id`; a live entry keeps the diff it
 *   received, which the history does not carry) and keeps the live ones it does not have yet, after
 *   it, in their order. Old turns without `turn_id` are keyed by their position and time.
 */

import type { ChatRef } from "../../chat/api";
import type { HistoryTurn, SendOutcome, SpokenSpan, TurnOrigin, TurnOutcome, WorkspaceEvent, WorkspaceHistory } from "./api";
import { DISCONNECTED_TURN } from "./api";

export type TurnStatus = "queued" | "running" | "done" | "failed";

export interface ChatEntry {
  key: string;
  turnId: string | null;
  requestId: string | null;
  origin: TurnOrigin;
  /** `revise`, `explain`, `prepare_notes` or the kind of a spoken request (`edit`, `question`...). */
  kind: string;
  status: TurnStatus;
  /** A typed message (a voice turn: the raw transcript); `null` while unknown. */
  message: string | null;
  requestSummary: string | null;
  transcript: SpokenSpan | null;
  /** ISO time of the turn, when known. */
  time: string | null;
  /** The reply streamed so far, or the final one. */
  reply: string;
  attempt: number;
  applied: boolean;
  /** The applied change's summary. */
  summary: string | null;
  changedSections: string[];
  diff: string | null;
  commit: string | null;
  undone: boolean;
  warning: string | null;
  refs: ChatRef[];
  failure: string | null;
  /** The turn stopped at a reached cost cap: "Continuar igualmente" repeats it confirmed. */
  overCap: boolean;
  /** A send of this page runs for the entry; its own stream feeds it until a turn is adopted. */
  local: boolean;
  /** A typed turn whose own stream broke before any turn was adopted: the history may have it. */
  orphan: boolean;
  fromHistory: boolean;
}

export interface ChatState {
  entries: ChatEntry[];
  canUndo: boolean;
}

export const INITIAL_CHAT: ChatState = { entries: [], canUndo: false };

export type ChatAction =
  | { type: "history"; history: WorkspaceHistory }
  | { type: "event"; event: WorkspaceEvent }
  /** A send starts; `replace` reuses that entry (a retry over the cost cap). */
  | { type: "local.start"; key: string; message: string; kind?: string; time: string; replace?: string }
  | { type: "local.delta"; key: string; text: string; attempt: number }
  | { type: "local.restart"; key: string; attempt: number }
  | { type: "local.done"; key: string; result: SendOutcome }
  | { type: "undone"; commit: string };

function blank(key: string, fields: Partial<ChatEntry>): ChatEntry {
  return {
    key,
    turnId: null,
    requestId: null,
    origin: "typed",
    kind: "revise",
    status: "running",
    message: null,
    requestSummary: null,
    transcript: null,
    time: null,
    reply: "",
    attempt: 1,
    applied: false,
    summary: null,
    changedSections: [],
    diff: null,
    commit: null,
    undone: false,
    warning: null,
    refs: [],
    failure: null,
    overCap: false,
    local: false,
    orphan: false,
    fromHistory: false,
    ...fields,
  };
}

function fromHistory(turn: HistoryTurn, index: number, live: ChatEntry | undefined): ChatEntry {
  return blank(turn.turnId ?? `h${index}-${turn.time}`, {
    key: live?.key ?? turn.turnId ?? `h${index}-${turn.time}`,
    turnId: turn.turnId,
    requestId: turn.transcript?.requestId ?? null,
    origin: turn.origin,
    kind: turn.kind,
    status: "done",
    message: turn.message,
    requestSummary: turn.requestSummary,
    transcript: turn.transcript,
    time: turn.time || null,
    reply: turn.reply,
    applied: turn.applied,
    summary: turn.summary,
    changedSections: turn.changedSections,
    diff: live?.diff ?? null,
    commit: turn.commit,
    undone: turn.undone,
    warning: turn.warning,
    refs: turn.refs,
    fromHistory: true,
  });
}

function mergeHistory(state: ChatState, history: WorkspaceHistory): ChatState {
  const byTurn = new Map(state.entries.filter((e) => e.turnId !== null).map((e) => [e.turnId as string, e]));
  const turns = history.turns.map((turn, index) => fromHistory(turn, index, turn.turnId !== null ? byTurn.get(turn.turnId) : undefined));
  const turnIds = new Set(turns.flatMap((t) => (t.turnId !== null ? [t.turnId] : [])));
  const requestIds = new Set(turns.flatMap((t) => (t.requestId !== null ? [t.requestId] : [])));
  const live = state.entries.filter(
    (e) =>
      !e.fromHistory &&
      !(e.turnId !== null && turnIds.has(e.turnId)) &&
      !(e.requestId !== null && requestIds.has(e.requestId) && !e.local) &&
      !(e.orphan && turns.some((t) => t.origin === "typed" && t.message === e.message)),
  );
  return { entries: [...turns, ...live], canUndo: history.canUndo };
}

const update = (state: ChatState, key: string, change: (entry: ChatEntry) => Partial<ChatEntry>): ChatState => ({
  ...state,
  entries: state.entries.map((entry) => (entry.key === key ? { ...entry, ...change(entry) } : entry)),
});

/** The entry of `turnId` (else of `requestId` and not yet a turn); created when there is none. */
function attach(state: ChatState, turnId: string | null, requestId: string | null, create: Partial<ChatEntry>): [ChatState, ChatEntry] {
  const found =
    (turnId !== null ? state.entries.find((e) => e.turnId === turnId) : undefined) ??
    (requestId !== null ? state.entries.find((e) => e.requestId === requestId && e.turnId === null) : undefined);
  if (found !== undefined) {
    if (turnId === null || found.turnId === turnId) return [state, found];
    const bound = { ...found, turnId };
    return [update(state, found.key, () => ({ turnId })), bound];
  }
  const key = turnId ?? (requestId !== null ? `req:${requestId}` : `unknown${state.entries.length}`);
  const entry = blank(key, { turnId, requestId, ...create });
  return [{ ...state, entries: [...state.entries, entry] }, entry];
}

function applyOutcome(entry: ChatEntry, outcome: TurnOutcome): Partial<ChatEntry> {
  return {
    status: "done",
    origin: outcome.requestSummary !== null || outcome.transcript !== null ? "voice" : entry.origin,
    kind: outcome.kind === "revise" && entry.kind !== "revise" ? entry.kind : outcome.kind,
    message: entry.origin === "voice" ? (entry.message ?? outcome.message) : (outcome.message ?? entry.message),
    requestSummary: entry.requestSummary ?? outcome.requestSummary,
    transcript: entry.transcript ?? outcome.transcript,
    requestId: entry.requestId ?? outcome.requestId,
    reply: outcome.reply,
    applied: outcome.applied,
    summary: outcome.summary,
    changedSections: outcome.changedSections,
    diff: outcome.diff ?? entry.diff,
    commit: outcome.commit,
    warning: outcome.warning,
    failure: null,
    overCap: false,
  };
}

const undoable = (outcome: TurnOutcome) => outcome.kind !== "prepare_notes" && outcome.applied && outcome.commit !== null;

function onEvent(state: ChatState, event: WorkspaceEvent): ChatState {
  switch (event.type) {
    case "request.detected": {
      if (state.entries.some((e) => e.requestId === event.requestId)) return state;
      const entry = blank(`req:${event.requestId}`, {
        requestId: event.requestId,
        origin: "voice",
        kind: event.kind,
        status: "queued",
        requestSummary: event.summary || null,
        transcript: event.transcript,
        message: event.transcript?.text ?? null,
        time: new Date().toISOString(),
      });
      return { ...state, entries: [...state.entries, entry] };
    }
    case "turn.started": {
      if (state.entries.some((e) => e.turnId === event.turnId)) return state;
      const byRequest =
        event.requestId !== null ? state.entries.find((e) => e.requestId === event.requestId && e.turnId === null) : undefined;
      const adoptable =
        byRequest ??
        (event.origin === "typed" ? state.entries.find((e) => e.local && e.turnId === null && e.status === "running") : undefined);
      if (adoptable !== undefined) {
        return update(state, adoptable.key, (e) => ({
          turnId: event.turnId,
          status: "running",
          kind: e.origin === "voice" && event.kind === "revise" ? e.kind : event.kind,
          // From now on the workspace stream writes the reply, from its start.
          reply: "",
          attempt: 1,
        }));
      }
      const entry = blank(event.turnId, {
        turnId: event.turnId,
        requestId: event.requestId,
        origin: event.origin,
        kind: event.kind,
        time: new Date().toISOString(),
      });
      return { ...state, entries: [...state.entries, entry] };
    }
    case "reply.delta": {
      const [next, entry] = attach(state, event.turnId, null, { origin: "typed" });
      if (entry.status === "done" || entry.status === "failed" || event.attempt < entry.attempt) return next;
      return update(next, entry.key, (e) => ({
        status: "running",
        attempt: event.attempt,
        reply: (event.attempt > e.attempt ? "" : e.reply) + event.text,
      }));
    }
    case "reply.restart": {
      const [next, entry] = attach(state, event.turnId, null, { origin: "typed" });
      if (entry.status === "done" || entry.status === "failed" || event.attempt < entry.attempt) return next;
      return update(next, entry.key, () => ({ attempt: event.attempt, reply: "" }));
    }
    case "turn.result": {
      const { outcome } = event;
      const [next, entry] = attach(state, event.turnId, outcome.requestId, {
        origin: outcome.requestSummary !== null || outcome.transcript !== null ? "voice" : "typed",
      });
      const done = update(next, entry.key, (e) => applyOutcome(e, outcome));
      return undoable(outcome) ? { ...done, canUndo: true } : done;
    }
    case "turn.error": {
      const [next, entry] = attach(state, event.turnId, event.requestId, { origin: event.requestId !== null ? "voice" : "typed" });
      return update(next, entry.key, () => ({ status: "failed", failure: event.detail, overCap: event.overCap }));
    }
    case "notes.changed":
      return state;
  }
}

/** Moves a wrongly adopted turn out of `entry` into an entry of its own. */
function splitOff(state: ChatState, entry: ChatEntry): ChatState {
  if (entry.turnId === null) return state;
  const other = blank(entry.turnId, {
    turnId: entry.turnId,
    status: entry.status === "failed" ? "failed" : "running",
    reply: entry.reply,
    attempt: entry.attempt,
    time: entry.time,
  });
  const at = state.entries.findIndex((e) => e.key === entry.key);
  const entries = [...state.entries];
  entries.splice(at, 0, other);
  return { ...state, entries };
}

function onLocalDone(state: ChatState, key: string, result: SendOutcome): ChatState {
  const entry = state.entries.find((e) => e.key === key);
  if (entry === undefined) return state;
  if (result.kind === "ok") {
    const { outcome } = result;
    let next = state;
    if (entry.turnId !== null && outcome.turnId !== null && entry.turnId !== outcome.turnId) next = splitOff(next, entry);
    if (outcome.turnId !== null) {
      // An entry the workspace stream made for this turn before it was told whose it was.
      next = { ...next, entries: next.entries.filter((e) => e.key === key || e.turnId !== outcome.turnId) };
    }
    next = update(next, key, (e) => ({ ...applyOutcome(e, outcome), turnId: outcome.turnId ?? e.turnId, local: false }));
    return undoable(outcome) ? { ...next, canUndo: true } : next;
  }
  if (result.kind === "failed") {
    const { failure } = result;
    let next = state;
    if (failure.beforeTurn && entry.turnId !== null) next = splitOff(next, entry);
    return update(next, key, (e) => ({
      local: false,
      status: "failed",
      turnId: failure.beforeTurn ? null : e.turnId,
      failure: failure.detail,
      overCap: failure.overCap,
    }));
  }
  // Interrupted: an adopted turn goes on through the workspace stream; otherwise the history
  // (read again by the caller) shows what the backend kept.
  if (entry.turnId !== null && entry.status === "running") return update(state, key, () => ({ local: false }));
  if (entry.status === "done") return update(state, key, () => ({ local: false }));
  return update(state, key, () => ({ local: false, orphan: true, status: "failed", failure: DISCONNECTED_TURN }));
}

export function reduceChat(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "history":
      return mergeHistory(state, action.history);
    case "event":
      return onEvent(state, action.event);
    case "local.start": {
      const fresh = {
        turnId: null,
        status: "running" as const,
        reply: "",
        attempt: 1,
        failure: null,
        overCap: false,
        local: true,
        orphan: false,
        applied: false,
        summary: null,
        diff: null,
        commit: null,
        warning: null,
      };
      if (action.replace !== undefined && state.entries.some((e) => e.key === action.replace)) {
        return update(state, action.replace, () => fresh);
      }
      const entry = blank(action.key, { ...fresh, kind: action.kind ?? "revise", message: action.message, time: action.time });
      return { ...state, entries: [...state.entries, entry] };
    }
    case "local.delta": {
      const entry = state.entries.find((e) => e.key === action.key);
      if (entry === undefined || !entry.local || entry.turnId !== null || action.attempt < entry.attempt) return state;
      return update(state, action.key, (e) => ({
        attempt: action.attempt,
        reply: (action.attempt > e.attempt ? "" : e.reply) + action.text,
      }));
    }
    case "local.restart": {
      const entry = state.entries.find((e) => e.key === action.key);
      if (entry === undefined || !entry.local || entry.turnId !== null) return state;
      return update(state, action.key, () => ({ attempt: action.attempt, reply: "" }));
    }
    case "local.done":
      return onLocalDone(state, action.key, action.result);
    case "undone":
      return {
        ...state,
        entries: state.entries.map((e) => (e.commit === action.commit ? { ...e, undone: true } : e)),
      };
  }
}
