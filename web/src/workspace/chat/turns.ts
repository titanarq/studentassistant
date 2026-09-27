/**
 * The workspace chat's list of turns (#317, #329) as a pure reducer, so the merge rules are in one
 * place: the history (`GET .../notes/chat`), the live events of the workspace stream and what this
 * page sends.
 *
 * - A turn is keyed by its `turn_id`. A request shows as soon as it is known -- a spoken one on
 *   `request.detected`, a typed one when `POST .../workspace/messages` answers its requests (or on
 *   its own `request.detected`, whichever comes first) -- as "En cola…" by `request_id`, in
 *   arrival order, and becomes its turn on `turn.started`. A typed message shows «Respondiendo…»
 *   until the POST answers.
 * - "Continuar igualmente" past the cost cap (#351) confirms the stopped turn through `POST
 *   .../workspace/messages` `{confirm_over_cap, turn_id}`: the entry goes back to "En cola…"
 *   without its turn, and the request's new `turn.started` (same `request_id`) takes it again.
 * - A whole-topic run ("prepárame el tema", batched, #326) is one entry of kind `prepare_notes`
 *   with its `incorporation.progress`; each batch (an `incorporate` turn without request) is an
 *   entry whose `parent` is that run (a run started elsewhere gets an entry of its own).
 * - Doubts asked in the chat are entries keyed `doubt:<pending_id>` (`doubt.asked`, updated by
 *   `doubt.resolved`); the editor's auto-resolutions `auto:<pending_ids>`.
 * - A history read replaces every entry it has (by `turn_id`, else by those keys; a live entry
 *   keeps its key, so nothing is announced twice, and the diff it received) and keeps the live
 *   ones it does not have yet, after it, in their order. An answer to a doubt that the history
 *   shows answered is dropped (the doubt entry says what was answered); a queued typed request
 *   whose turn the history already has (typed turns carry no `request_id`) goes too. Old turns
 *   without `turn_id` are keyed by their position and time.
 */

import type { ChatRef } from "../../chat/api";
import type {
  DoubtView,
  GoStudyAction,
  HistoryTurn,
  PostOutcome,
  SpokenSpan,
  TriageDecision,
  TriageTarget,
  TurnOrigin,
  TurnOutcome,
  WorkspaceEvent,
  WorkspaceHistory,
} from "./api";

export type TurnStatus = "sending" | "queued" | "running" | "done" | "failed";

/** Who a request came from: the student (typed, voice) or the assistant on its own (`system`). */
export type EntryOrigin = TurnOrigin | "system";

export interface ChatEntry {
  key: string;
  turnId: string | null;
  requestId: string | null;
  origin: EntryOrigin;
  /**
   * `revise`, `explain`, `prepare_notes`, `incorporate`, `triage`, `doubt_answer`, `doubt`,
   * `doubts_resolved`, or the kind of a spoken request (`edit`, `question`...).
   */
  kind: string;
  status: TurnStatus;
  /** A typed message (a voice turn: the raw transcript); `null` while unknown. */
  message: string | null;
  requestSummary: string | null;
  transcript: SpokenSpan | null;
  /** The typed message a request came from, when the POST said so. */
  messageId: string | null;
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
  fromHistory: boolean;
  /** `incorporate`: the sources incorporated; `triage`: the ones set aside or restored. */
  sourceIds: string[];
  /** `incorporate`: the doubts it raised (known live only). */
  doubts: number;
  decision: TriageDecision | null;
  /** `triage` (`set_aside`): each target and its triage reasons. */
  targets: TriageTarget[];
  /** `doubt`: the doubt asked. */
  doubt: DoubtView | null;
  /** `doubt_answer`: the doubt answered. */
  pendingId: string | null;
  /** `prepare_notes` in batches: how far the run is. */
  progress: { done: number; total: number } | null;
  /** A batch of a whole-topic run: the run's entry key. */
  parent: string | null;
  /** `study`: the "Ir a Estudiar" button (live only; the history does not keep these turns). */
  action: GoStudyAction | null;
}

export interface ChatState {
  entries: ChatEntry[];
  canUndo: boolean;
}

export const INITIAL_CHAT: ChatState = { entries: [], canUndo: false };

export type ChatAction =
  | { type: "history"; history: WorkspaceHistory }
  | { type: "event"; event: WorkspaceEvent }
  /** A typed message goes to `POST .../workspace/messages`. */
  | { type: "post.start"; key: string; message: string; time: string }
  | { type: "post.done"; key: string; result: PostOutcome }
  /** "Continuar igualmente" on the entry: its turn is confirmed past the cost cap. */
  | { type: "confirm.start"; key: string }
  | { type: "confirm.done"; key: string; result: PostOutcome }
  | { type: "undone"; commit: string };

/** `set_aside` / `restore` turns are one kind in the chat, as in the history. */
export function entryKind(kind: string): string {
  return kind === "set_aside" || kind === "restore" ? "triage" : kind;
}

/** The kinds "Continuar igualmente" can repeat confirmed (every one that calls Claude). */
const RETRYABLE = new Set(["revise", "edit", "question", "prepare_notes", "incorporate", "doubt_answer"]);

/** A request's turn stopped at the cost cap: the backend keeps it for a confirmation. */
export function canRetry(entry: ChatEntry): boolean {
  return entry.overCap && entry.parent === null && entry.turnId !== null && entry.requestId !== null && RETRYABLE.has(entry.kind);
}

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
    messageId: null,
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
    fromHistory: false,
    sourceIds: [],
    doubts: 0,
    decision: null,
    targets: [],
    doubt: null,
    pendingId: null,
    progress: null,
    parent: null,
    action: null,
    ...fields,
  };
}

/**
 * An entry still on its way on this page (sent, queued, running). Request ids are only unique
 * among these: typed ones restart in every session (`req-t1`…, #452), so a finished entry with
 * the same id is an older request, never this one.
 */
export const isOpen = (entry: ChatEntry): boolean =>
  !entry.fromHistory && (entry.status === "sending" || entry.status === "queued" || entry.status === "running");

const doubtKey = (pendingId: string) => `doubt:${pendingId}`;
const autoKey = (pendingIds: string[]) => `auto:${pendingIds.join(",")}`;

function historyKey(turn: HistoryTurn, index: number): string {
  if (turn.kind === "doubt" && turn.doubt !== null) return doubtKey(turn.doubt.pendingId);
  if (turn.kind === "doubts_resolved" && turn.pendingIds.length > 0) return autoKey(turn.pendingIds);
  return turn.turnId ?? `h${index}-${turn.time}`;
}

function fromHistory(turn: HistoryTurn, key: string, live: ChatEntry | undefined, liveKeys: Set<string>): ChatEntry {
  const system = turn.kind === "doubt" || turn.kind === "doubts_resolved";
  return blank(key, {
    key: live?.key ?? key,
    turnId: turn.turnId,
    requestId: turn.transcript?.requestId ?? null,
    origin: system ? "system" : turn.origin,
    kind: entryKind(turn.kind),
    status: "done",
    message: turn.message,
    requestSummary: turn.requestSummary,
    transcript: turn.transcript,
    messageId: live?.messageId ?? null,
    time: turn.time || null,
    reply: turn.reply,
    applied: turn.applied,
    summary: turn.summary,
    changedSections: turn.changedSections,
    diff: live?.diff ?? turn.diff,
    commit: turn.commit,
    undone: turn.undone,
    warning: turn.warning,
    refs: turn.refs,
    fromHistory: true,
    sourceIds: turn.sourceIds,
    doubts: live?.doubts ?? 0,
    decision: turn.decision,
    targets: turn.targets,
    doubt: turn.doubt,
    progress: live?.progress ?? null,
    parent: live?.parent != null && liveKeys.has(live.parent) ? live.parent : null,
  });
}

/** How long before a queued typed request its history turn may be dated (clock skew). */
const SKEW_MS = 60_000;

function mergeHistory(state: ChatState, history: WorkspaceHistory): ChatState {
  const byTurn = new Map(state.entries.filter((e) => e.turnId !== null).map((e) => [e.turnId as string, e]));
  const byKey = new Map(state.entries.map((e) => [e.key, e]));
  const liveKeys = new Set(state.entries.map((e) => e.key));
  const turns = history.turns.map((turn, index) => {
    const key = historyKey(turn, index);
    const live = (turn.turnId !== null ? byTurn.get(turn.turnId) : undefined) ?? byKey.get(key);
    return fromHistory(turn, key, live, liveKeys);
  });
  const keys = new Set(turns.map((t) => t.key));
  const turnIds = new Set(turns.flatMap((t) => (t.turnId !== null ? [t.turnId] : [])));
  const requestIds = new Set(turns.flatMap((t) => (t.requestId !== null ? [t.requestId] : [])));
  const answered = new Set(turns.flatMap((t) => (t.doubt !== null && t.doubt.status !== "open" ? [t.doubt.pendingId] : [])));
  // Typed turns the history has, each able to stand for one queued typed request.
  const typed = turns.filter((t) => t.origin === "typed" && t.message !== null && t.message !== "" && t.turnId !== null);
  const claimed = new Set<ChatEntry>();
  const doneElsewhere = (e: ChatEntry): boolean => {
    if (e.origin !== "typed" || e.turnId !== null || e.requestId === null || e.status !== "queued") return false;
    const since = e.time !== null ? Date.parse(e.time) - SKEW_MS : Number.NEGATIVE_INFINITY;
    const match = typed.find(
      (t) => !claimed.has(t) && t.message === e.message && (t.time === null || !(Date.parse(t.time) < since)) && !byTurn.has(t.turnId as string),
    );
    if (match === undefined) return false;
    claimed.add(match);
    return true;
  };
  const live = state.entries.filter(
    (e) =>
      !e.fromHistory &&
      !keys.has(e.key) &&
      !(e.turnId !== null && turnIds.has(e.turnId)) &&
      !(e.requestId !== null && requestIds.has(e.requestId)) &&
      !(e.kind === "doubt_answer" && e.pendingId !== null && e.status === "done" && answered.has(e.pendingId)) &&
      !doneElsewhere(e),
  );
  return { entries: [...turns, ...live], canUndo: history.canUndo };
}

const update = (state: ChatState, key: string, change: (entry: ChatEntry) => Partial<ChatEntry>): ChatState => ({
  ...state,
  entries: state.entries.map((entry) => (entry.key === key ? { ...entry, ...change(entry) } : entry)),
});

const append = (state: ChatState, entry: ChatEntry): ChatState => ({ ...state, entries: [...state.entries, entry] });

/** The entry of `turnId` (else of `requestId` and not yet a turn); created when there is none. */
function attach(state: ChatState, turnId: string | null, requestId: string | null, create: Partial<ChatEntry>): [ChatState, ChatEntry] {
  const found =
    (turnId !== null ? state.entries.find((e) => e.turnId === turnId) : undefined) ??
    (requestId !== null ? state.entries.find((e) => e.requestId === requestId && e.turnId === null && isOpen(e)) : undefined);
  if (found !== undefined) {
    if (turnId === null || found.turnId === turnId) return [state, found];
    const bound = { ...found, turnId };
    return [update(state, found.key, () => ({ turnId })), bound];
  }
  const key = turnId ?? (requestId !== null ? `req:${requestId}` : `unknown${state.entries.length}`);
  const entry = blank(key, { turnId, requestId, ...create });
  return [append(state, entry), entry];
}

function applyOutcome(entry: ChatEntry, outcome: TurnOutcome): Partial<ChatEntry> {
  const kind = entryKind(outcome.kind);
  return {
    status: "done",
    origin: outcome.requestSummary !== null || outcome.transcript !== null ? "voice" : entry.origin,
    kind: kind === "revise" && entry.kind !== "revise" ? entry.kind : kind,
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
    sourceIds: outcome.sourceIds.length > 0 ? outcome.sourceIds : entry.sourceIds,
    doubts: outcome.doubts,
    decision: outcome.decision ?? entry.decision,
    targets: outcome.targets.length > 0 ? outcome.targets : entry.targets,
    pendingId: outcome.pendingId ?? entry.pendingId,
    action: outcome.action,
  };
}

const undoable = (outcome: TurnOutcome) =>
  (outcome.kind === "revise" || outcome.kind === "incorporate") && outcome.applied && outcome.commit !== null;

/** The whole-topic run a batch or a progress belongs to: the latest one not finished. */
function openRun(state: ChatState): ChatEntry | undefined {
  const runs = state.entries.filter(
    (e) => e.kind === "prepare_notes" && e.parent === null && e.status === "running",
  );
  return runs.at(-1);
}

function onTurnStarted(state: ChatState, event: Extract<WorkspaceEvent, { type: "turn.started" }>): ChatState {
  if (state.entries.some((e) => e.turnId === event.turnId)) return state;
  const kind = entryKind(event.kind);
  const adoptable =
    event.requestId !== null ? state.entries.find((e) => e.requestId === event.requestId && e.turnId === null && isOpen(e)) : undefined;
  if (adoptable !== undefined) {
    return update(state, adoptable.key, (e) => ({
      turnId: event.turnId,
      status: "running",
      kind: e.origin === "voice" && kind === "revise" ? e.kind : kind,
      // From now on the workspace stream writes the reply, from its start.
      reply: "",
      attempt: 1,
    }));
  }
  let next = state;
  let parent: string | null = null;
  if (kind === "incorporate" && event.requestId === null) {
    // A batch of a whole-topic run: below that run's entry (one of its own when started elsewhere).
    let run = openRun(next);
    if (run === undefined) {
      run = blank(`run:${event.turnId}`, { origin: "system", kind: "prepare_notes", time: new Date().toISOString() });
      next = append(next, run);
    }
    parent = run.key;
  }
  return append(
    next,
    blank(event.turnId, {
      turnId: event.turnId,
      requestId: event.requestId,
      origin: event.origin,
      kind,
      time: new Date().toISOString(),
      parent,
    }),
  );
}

function onEvent(state: ChatState, event: WorkspaceEvent): ChatState {
  switch (event.type) {
    case "request.detected": {
      if (state.entries.some((e) => e.requestId === event.requestId && isOpen(e))) return state;
      const said = event.transcript?.text.trim() ?? "";
      const sending =
        event.origin === "typed"
          ? state.entries.find((e) => e.status === "sending" && e.requestId === null && e.message !== null && e.message === said)
          : undefined;
      if (sending !== undefined) {
        // The typed message this page is still posting: it becomes that request in place.
        return update(state, sending.key, () => ({
          requestId: event.requestId,
          kind: entryKind(event.kind),
          status: "queued",
          requestSummary: event.summary || null,
        }));
      }
      return append(
        state,
        blank(`req:${event.requestId}`, {
          requestId: event.requestId,
          origin: event.origin,
          kind: entryKind(event.kind),
          status: "queued",
          requestSummary: event.summary || null,
          transcript: event.origin === "voice" ? event.transcript : null,
          message: event.transcript?.text ?? null,
          time: new Date().toISOString(),
        }),
      );
    }
    case "turn.started":
      return onTurnStarted(state, event);
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
    case "notes.changed": {
      if (event.origin !== "generation") return state;
      // A run started elsewhere has no result of its own: the generation's change closes it.
      return {
        ...state,
        entries: state.entries.map((e) =>
          e.kind === "prepare_notes" && e.origin === "system" && e.status === "running" ? { ...e, status: "done" } : e,
        ),
      };
    }
    case "incorporation.progress": {
      const run = openRun(state);
      if (run === undefined) return state;
      const finished = run.origin === "system" && event.total > 0 && event.done >= event.total;
      return update(state, run.key, () => ({
        progress: { done: event.done, total: event.total },
        ...(finished ? { status: "done" as const } : {}),
      }));
    }
    case "doubt.asked": {
      const key = doubtKey(event.doubt.pendingId);
      if (state.entries.some((e) => e.key === key)) {
        return update(state, key, (e) => ({ doubt: { ...event.doubt, answer: e.doubt?.answer ?? null } }));
      }
      return append(
        state,
        blank(key, {
          origin: "system",
          kind: "doubt",
          status: "done",
          reply: event.doubt.question,
          doubt: event.doubt,
          time: new Date().toISOString(),
        }),
      );
    }
    case "doubt.resolved":
      return update(state, doubtKey(event.pendingId), (e) => ({
        doubt: e.doubt === null ? null : { ...e.doubt, status: event.status, resolution: event.resolution },
        applied: event.notesChanged,
      }));
    case "doubts.auto_resolved": {
      const ids = new Set(event.pendingIds);
      const settled: ChatState = {
        ...state,
        entries: state.entries.map((e) =>
          e.doubt !== null && ids.has(e.doubt.pendingId) && e.doubt.status === "open"
            ? { ...e, doubt: { ...e.doubt, status: "auto_resolved" } }
            : e,
        ),
      };
      const key = autoKey(event.pendingIds);
      if (event.summary === "" || settled.entries.some((e) => e.key === key)) return settled;
      return append(
        settled,
        blank(key, { origin: "system", kind: "doubts_resolved", status: "done", reply: event.summary, time: new Date().toISOString() }),
      );
    }
  }
}

/**
 * The POST answered: its requests take the sending entry's place (or bind the ones already
 * shown). The student's message never disappears: the entry stays unless another entry of this
 * page already shows the same request, and a POST with no request keeps it, with a failure.
 */
function onPostDone(state: ChatState, key: string, result: PostOutcome): ChatState {
  const at = state.entries.findIndex((e) => e.key === key);
  if (at < 0) return state;
  const sending = state.entries[at];
  if (result.kind === "failed") {
    return update(state, key, () => ({ status: "failed", failure: result.detail }));
  }
  if (result.requests.length === 0) {
    return update(state, key, () => ({ status: "failed", failure: "El asistente no ha recibido el mensaje. Vuelve a enviarlo." }));
  }
  const ids = new Set(result.requests.map((request) => request.requestId));
  // Requests another open entry already shows (announced on the stream before this answer).
  const shown = new Set(
    state.entries.flatMap((e) => (e.key !== key && e.requestId !== null && isOpen(e) && ids.has(e.requestId) ? [e.requestId] : [])),
  );
  const own = sending.requestId;
  const fresh = result.requests.filter((request) => !shown.has(request.requestId) && request.requestId !== own);
  const entries = state.entries.map((e) =>
    e.key !== key && e.requestId !== null && shown.has(e.requestId) ? { ...e, messageId: result.messageId } : e,
  );
  const make = (request: (typeof fresh)[number], entryKey: string): ChatEntry =>
    blank(entryKey, {
      requestId: request.requestId,
      origin: "typed",
      kind: entryKind(request.kind),
      status: "queued",
      message: request.text || sending.message,
      requestSummary: request.summary || null,
      messageId: result.messageId,
      time: sending.time,
    });
  let replacement: ChatEntry[];
  if (own !== null) {
    // Already bound by its `request.detected`: it stays, the other requests follow it.
    replacement = [{ ...sending, messageId: result.messageId }, ...fresh.map((r) => make(r, `req:${r.requestId}`))];
  } else if (fresh.length > 0) {
    // The first keeps the sending entry's key, so its element stays.
    replacement = fresh.map((r, index) => make(r, index === 0 ? sending.key : `req:${r.requestId}`));
  } else {
    replacement = [];
  }
  entries.splice(at, 1, ...replacement);
  return { ...state, entries };
}

export function reduceChat(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "history":
      return mergeHistory(state, action.history);
    case "event":
      return onEvent(state, action.event);
    case "post.start":
      return append(state, blank(action.key, { status: "sending", origin: "typed", message: action.message, time: action.time }));
    case "post.done":
      return onPostDone(state, action.key, action.result);
    case "confirm.start":
      // Back to the queue without its turn: the request's new `turn.started` takes it again.
      return update(state, action.key, () => ({
        turnId: null,
        status: "queued",
        reply: "",
        attempt: 1,
        failure: null,
        overCap: false,
        warning: null,
      }));
    case "confirm.done": {
      const { result } = action;
      if (result.kind === "ok") return state;
      return update(state, action.key, () => ({ status: "failed", failure: result.detail }));
    }
    case "undone":
      return {
        ...state,
        entries: state.entries.map((e) => (e.commit === action.commit ? { ...e, undone: true } : e)),
      };
  }
}
