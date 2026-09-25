/**
 * The live session view's client (#57): the `GET /api/live` Server-Sent Events stream of the
 * backend (docs/modules/server.md) and the pure reducer that folds its events into what the page
 * shows. Read-only: nothing here sends anything back.
 *
 * The stream follows the backend's active session: a `snapshot` first (`session: null` when none
 * is running; the stream then ends and `EventSource` reconnects on its own), then `segment`,
 * `partial`, `capture` and `outline` events, and `ended` when the session ends.
 */

export const LIVE_URL = "/api/live";

export interface LiveSession {
  session_id: string;
  subject_id: string;
  topic_id: string;
  started_at_ms: number;
}

export interface LiveSegment {
  segment_id: string;
  /** Session milliseconds. */
  t_start: number;
  t_end: number;
  text: string;
}

export type CaptureStatus = "pending" | "transcribed" | "failed";

export interface LiveCapture {
  capture_id: string;
  t: number | null;
  /** Vault-relative path of the flattened page image (for `GET /api/sources/...`). */
  page_path: string | null;
  source_path: string | null;
  source_context: string | null;
  status: CaptureStatus;
  text: string | null;
  page_number: number | null;
  message: string | null;
}

export interface LiveSection {
  section_id: string;
  title: string;
  parent_id: string | null;
  segment_count: number;
}

/**
 * What the page shows. `phase`: `connecting` before the first snapshot, `idle` when no session
 * runs (and none was followed), `live` while one runs, `ended` after it ended (its data stays).
 */
export interface LiveState {
  phase: "connecting" | "idle" | "live" | "ended";
  session: LiveSession | null;
  segments: LiveSegment[];
  partial: LiveSegment | null;
  captures: LiveCapture[];
  outline: LiveSection[];
  openPending: number;
}

export type LiveEvent =
  | {
      type: "snapshot";
      session: LiveSession | null;
      segments: LiveSegment[];
      captures: LiveCapture[];
      outline: LiveSection[];
      open_pending: number;
    }
  | { type: "segment"; segment: LiveSegment }
  | { type: "partial"; segment: LiveSegment }
  | { type: "capture"; capture: LiveCapture }
  | { type: "outline"; outline: LiveSection[]; open_pending: number }
  | { type: "ended"; session_id: string };

export const INITIAL_STATE: LiveState = {
  phase: "connecting",
  session: null,
  segments: [],
  partial: null,
  captures: [],
  outline: [],
  openPending: 0,
};

// -- lenient decoding: a malformed event is dropped, never thrown ------------------------------

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function int(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readSession(value: unknown): LiveSession | null {
  if (!isObject(value)) return null;
  const session_id = str(value.session_id);
  const subject_id = str(value.subject_id);
  const topic_id = str(value.topic_id);
  const started_at_ms = int(value.started_at_ms);
  if (session_id === null || subject_id === null || topic_id === null || started_at_ms === null) return null;
  return { session_id, subject_id, topic_id, started_at_ms };
}

function readSegment(value: unknown): LiveSegment | null {
  if (!isObject(value)) return null;
  const segment_id = str(value.segment_id);
  const text = str(value.text);
  const t_start = int(value.t_start);
  const t_end = int(value.t_end) ?? t_start;
  if (segment_id === null || text === null || t_start === null || t_end === null) return null;
  return { segment_id, t_start, t_end, text };
}

const STATUSES: readonly CaptureStatus[] = ["pending", "transcribed", "failed"];

function readCapture(value: unknown): LiveCapture | null {
  if (!isObject(value)) return null;
  const capture_id = str(value.capture_id);
  if (capture_id === null) return null;
  const status = STATUSES.find((s) => s === value.status) ?? "pending";
  return {
    capture_id,
    t: int(value.t),
    page_path: str(value.page_path),
    source_path: str(value.source_path),
    source_context: str(value.source_context),
    status,
    text: str(value.text),
    page_number: int(value.page_number),
    message: str(value.message),
  };
}

function readSection(value: unknown): LiveSection | null {
  if (!isObject(value)) return null;
  const section_id = str(value.section_id);
  const title = str(value.title);
  if (section_id === null || title === null) return null;
  return { section_id, title, parent_id: str(value.parent_id), segment_count: int(value.segment_count) ?? 0 };
}

function list<T>(value: unknown, read: (item: unknown) => T | null): T[] {
  if (!Array.isArray(value)) return [];
  return value.map(read).filter((item): item is T => item !== null);
}

/** One stream event (`event:` name and its JSON `data:`) as a `LiveEvent`; null when unusable. */
export function parseLiveEvent(name: string, data: string): LiveEvent | null {
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    return null;
  }
  if (!isObject(value)) return null;
  switch (name) {
    case "snapshot":
      return {
        type: "snapshot",
        session: readSession(value.session),
        segments: list(value.segments, readSegment),
        captures: list(value.captures, readCapture),
        outline: list(value.outline, readSection),
        open_pending: int(value.open_pending) ?? 0,
      };
    case "segment":
    case "partial": {
      const segment = readSegment(value);
      return segment === null ? null : { type: name, segment };
    }
    case "capture": {
      const capture = readCapture(value);
      return capture === null ? null : { type: "capture", capture };
    }
    case "outline":
      return { type: "outline", outline: list(value.outline, readSection), open_pending: int(value.open_pending) ?? 0 };
    case "ended": {
      const session_id = str(value.session_id);
      return session_id === null ? null : { type: "ended", session_id };
    }
    default:
      return null;
  }
}

// -- the reducer -------------------------------------------------------------------------------

function upsertCapture(captures: LiveCapture[], capture: LiveCapture): LiveCapture[] {
  const at = captures.findIndex((c) => c.capture_id === capture.capture_id);
  if (at === -1) return [...captures, capture];
  const next = captures.slice();
  next[at] = capture;
  return next;
}

/**
 * Folds one event into the state. A snapshot replaces everything (each reconnect starts with
 * one), except that a snapshot without a session keeps what the page showed of the last one and
 * marks it ended. Events of a session other than the one shown are ignored.
 */
export function reduceLive(state: LiveState, event: LiveEvent): LiveState {
  if (event.type === "snapshot") {
    if (event.session === null) {
      return state.session === null ? { ...INITIAL_STATE, phase: "idle" } : { ...state, phase: "ended", partial: null };
    }
    return {
      phase: "live",
      session: event.session,
      segments: event.segments,
      partial: null,
      captures: event.captures,
      outline: event.outline,
      openPending: event.open_pending,
    };
  }
  if (state.phase !== "live") return state;
  switch (event.type) {
    case "segment": {
      if (state.segments.some((s) => s.segment_id === event.segment.segment_id)) return state;
      const partial = state.partial?.segment_id === event.segment.segment_id ? null : state.partial;
      return { ...state, segments: [...state.segments, event.segment], partial };
    }
    case "partial":
      if (state.segments.some((s) => s.segment_id === event.segment.segment_id)) return state;
      return { ...state, partial: event.segment };
    case "capture":
      return { ...state, captures: upsertCapture(state.captures, event.capture) };
    case "outline":
      return { ...state, outline: event.outline, openPending: event.open_pending };
    case "ended":
      if (state.session?.session_id !== event.session_id) return state;
      return { ...state, phase: "ended", partial: null };
  }
}

// -- the connection ----------------------------------------------------------------------------

/** The part of `EventSource` the page uses, so tests can hand it a fake. */
export interface LiveSource {
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
}

export type LiveSourceFactory = (url: string) => LiveSource;

export const defaultSource: LiveSourceFactory = (url) => new EventSource(url) as unknown as LiveSource;

const EVENT_NAMES = ["snapshot", "segment", "partial", "capture", "outline", "ended"] as const;

/**
 * Opens the stream and hands every usable event to `onEvent`; `onConnection(false)` when the
 * connection drops (the source reconnects by itself), `onConnection(true)` when it is back.
 * Returns the function that closes it.
 */
export function subscribeLive(
  onEvent: (event: LiveEvent) => void,
  onConnection: (connected: boolean) => void,
  factory: LiveSourceFactory = defaultSource,
): () => void {
  const source = factory(LIVE_URL);
  for (const name of EVENT_NAMES) {
    source.addEventListener(name, (message) => {
      const event = parseLiveEvent(name, message.data);
      if (event !== null) onEvent(event);
    });
  }
  source.onopen = () => onConnection(true);
  source.onerror = () => onConnection(false);
  return () => source.close();
}

/** How the page names the source a page was captured as. */
export function contextLabel(context: string | null): string {
  switch (context) {
    case "notes":
      return "Apuntes";
    case "book":
      return "Libro";
    case "pdf":
      return "PDF";
    case "web":
      return "Web";
    default:
      return "Página";
  }
}

export function statusLabel(capture: LiveCapture): string {
  switch (capture.status) {
    case "pending":
      return "Transcribiendo…";
    case "transcribed":
      return "Transcrita";
    case "failed":
      return capture.message ? `No se pudo transcribir: ${capture.message}` : "No se pudo transcribir";
  }
}

/** The outline as a tree: each section with its children, in the order the backend sent. */
export interface OutlineNode {
  section: LiveSection;
  children: OutlineNode[];
}

export function outlineTree(sections: LiveSection[]): OutlineNode[] {
  const nodes = new Map<string, OutlineNode>();
  for (const section of sections) nodes.set(section.section_id, { section, children: [] });
  const roots: OutlineNode[] = [];
  for (const section of sections) {
    const node = nodes.get(section.section_id)!;
    const parent = section.parent_id === null ? undefined : nodes.get(section.parent_id);
    if (parent === undefined || parent === node) roots.push(node);
    else parent.children.push(node);
  }
  return roots;
}
