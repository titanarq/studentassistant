/**
 * The capture page's session socket (#40). It opens the `ws_path` of the session start/resume
 * answer, sends `hello` as the first frame on the wire and negotiates with the backend's
 * `hello.ack`, which fixes the protocol version both sides speak, the clock offset and the STT
 * mode the page picks its transcriber from (protocol/README.md, "WebSocket `/ws/sessions/{id}`").
 * Every frame it sends is one of the bindings' `client.*` messages and every frame it receives
 * goes through `parseServerEvent`, so a message of any other shape is reported as `rejected`
 * instead of quietly used. The page runs on the PC itself, which the backend trusts while it
 * listens on loopback (docs/modules/server.md), so this socket carries no bearer token.
 */

import {
  type AudioFormat,
  type Button,
  type ButtonName,
  type ClientCapabilities,
  type ClientEvent,
  type Command,
  type HelloAck,
  IncompatibleProtocolVersionError,
  negotiate,
  type Notice,
  parseServerEvent,
  PROTOCOL_VERSION,
  ProtocolDecodeError,
  type ServerAck,
  type ServerEvent,
  type SourceKind,
  type SttStatus,
  type TranscriptClientFinal,
  type TranscriptClientPartial,
  type TranscriptFinal,
  type TranscriptPartial,
} from "../protocol";

/** The recognizer `hello` announces: the browser's own Web Speech API (ADR-0008). */
export const WEB_SPEECH_PROVIDER = "web-speech";

/** The audio this client can stream when the backend asks for server-side STT. */
export const CLIENT_AUDIO_FORMAT: AudioFormat = {
  encoding: "pcm16",
  sample_rate_hz: 16000,
  channels: 1,
};

/** What the capture page's `hello` announces: its own recognizer, plus audio if asked for. */
export const CAPTURE_CAPABILITIES: ClientCapabilities = {
  stt: "client",
  stt_provider: WEB_SPEECH_PROVIDER,
  audio_format: CLIENT_AUDIO_FORMAT,
};

/** Close codes this side sends: the student's clean end, and a frame that breaks protocol v1. */
const NORMAL_CLOSURE = 1000;
const PROTOCOL_ERROR = 1002;

/**
 * The backend's close code for a session that is not (or no longer) the active one
 * (`CLOSE_UNKNOWN_SESSION` in `studentassistant.server.ws`): after the handshake it means the
 * session ended, by this page's own Terminar or elsewhere, never that the backend went away.
 */
export const SESSION_NOT_ACTIVE_CLOSE = 4404;

/**
 * The absolute URL a browser socket dials for the `ws_path` of a session answer: the page's own
 * origin, `wss` for a page served over TLS and `ws` otherwise. `base` is the page's URL, and only
 * a test passes another one.
 */
export function socketUrl(wsPath: string, base: string = window.location.href): string {
  const url = new URL(wsPath, base);
  url.protocol = url.protocol === "https:" || url.protocol === "wss:" ? "wss:" : "ws:";
  return url.toString();
}

/** One utterance of the client's own recognizer, as a `transcript.client.*` frame carries it. */
export type ClientSegment = Omit<TranscriptClientFinal, "type">;

/** Which of the two client transcript messages a segment goes out as. */
export type TranscriptKind = "partial" | "final";

/** Everything the socket tells the page: a decoded server event, or the connection itself. */
export type SessionSocketEvent =
  /**
   * The backend's normalised transcript, in session time; a final replaces every partial with the
   * same `segment_id`. Narrow on `event.type` to show a partial grey and a final black.
   */
  | { kind: "transcript"; event: TranscriptPartial | TranscriptFinal }
  /** A voice command to act on (ADR-0006); `sendAck` answers it once the page has. */
  | { kind: "command"; event: Command }
  /** Status for the student, such as how many doubts await review. */
  | { kind: "notice"; event: Notice }
  /**
   * Since 1.5, server STT mode: the backend's own recognizer stopped or resumed transcribing the
   * audio this page streams. Sent once per change; `ok` clears the warning.
   */
  | { kind: "sttStatus"; event: SttStatus }
  /** The backend stored audio up to `audio_seq` and/or the listed captures. */
  | { kind: "ack"; event: ServerAck }
  /**
   * The socket ended. `wasClean` false (code 1006) is a lost backend connection; a clean close
   * after the page's own `close()` is the normal end of a session. With `reconnect` it is only
   * reported once the socket gives up; `SESSION_NOT_ACTIVE_CLOSE` then also stands for a resume
   * that answered the session is over, with the backend's `detail` as `reason`.
   */
  | { kind: "closed"; code: number; reason: string; wasClean: boolean }
  /**
   * The connection failed. A browser reports the `closed` event right after this one, so a lost
   * connection reaches the page twice and the message it shows has to cover either.
   */
  | { kind: "failed"; problem: string }
  /** A frame that is not a server event of protocol v1: never used, always reported. */
  | { kind: "rejected"; problem: string }
  /**
   * Since #411, with `reconnect`: the running session's connection dropped and the socket is
   * trying again. Reported once per outage; the next report is `reconnected` or a final `closed`.
   */
  | { kind: "reconnecting" }
  /** The outage is over: a new connection's `hello.ack` came and the kept frames went out. */
  | { kind: "reconnected"; ack: HelloAck };

/** How the `hello` / `hello.ack` negotiation ended; the page shows a message for anything but ok. */
export type HandshakeResult =
  | { kind: "ok"; ack: HelloAck; protocolVersion: string }
  /** The backend speaks another MAJOR version, or its first frame was no usable `hello.ack`. */
  | { kind: "rejected"; problem: string }
  /** The socket ended before the backend answered `hello`. */
  | { kind: "disconnected"; problem: string };

/**
 * What asking the backend to resume the session before a reconnect answered (#411): go ahead and
 * dial, the session is over (it ended elsewhere, or another one took its place), or the backend
 * is not there yet and the next attempt should ask again.
 */
export type ResumeOutcome =
  | { kind: "ok" }
  | { kind: "ended"; detail?: string }
  | { kind: "retry" };

/**
 * The delays between reconnect attempts, like the workspace chat's stream (`workspace/chat/
 * stream.ts`): the last one repeats for as long as the outage lasts, and a resumed session starts
 * over from the first.
 */
export const RECONNECT_DELAYS_MS: readonly number[] = [1000, 2000, 5000, 10000, 30000];

/** How many frames an outage keeps for after the resume; past it the oldest are dropped. */
export const MAX_QUEUED_FRAMES = 500;

export interface ReconnectOptions {
  /**
   * Called before every attempt: the capture page asks the backend to resume the session
   * (`POST /api/sessions/{id}/resume`), which a backend that restarted needs before it accepts
   * the socket again. Without it the socket just dials. A throw counts as `retry`.
   */
  resume?: () => Promise<ResumeOutcome>;
  /** The backoff; `RECONNECT_DELAYS_MS` by default. */
  delaysMs?: readonly number[];
  /** The client clock every reconnect's `hello.client_time_ms` is read from; `Date.now` by default. */
  clock?: () => number;
  /** The bound of the outage queue; `MAX_QUEUED_FRAMES` by default. */
  maxQueued?: number;
}

export interface SessionSocketOptions {
  /** The `ws_path` the session start/resume answer returned. */
  wsPath: string;
  /** The client clock reading `hello.client_time_ms` carries; the offset is measured against it. */
  clientTimeMs: number;
  /** What this client can do; the capture page's own capabilities by default. */
  capabilities?: ClientCapabilities;
  /** Called with every decoded server event and every change of the connection itself. */
  onEvent?: (event: SessionSocketEvent) => void;
  /**
   * Since #411: reconnect after an unexpected close instead of ending. Without it every close
   * ends the socket, as before.
   */
  reconnect?: ReconnectOptions;
}

/**
 * Close codes after which a new connection cannot help: the session is not the active one, or
 * the backend refused this client (a frame that breaks the protocol, another MAJOR version, no
 * trust). Anything else -- a lost connection (1006), a backend going away or restarting (1001,
 * 1012), an internal error (1011) -- is worth another try.
 */
const FINAL_CLOSE_CODES: ReadonlySet<number> = new Set([
  NORMAL_CLOSURE,
  PROTOCOL_ERROR,
  1003,
  1007,
  1008,
  SESSION_NOT_ACTIVE_CLOSE,
]);

/**
 * The client messages an outage keeps and sends after the resume, in order: what the student
 * said (finals; a partial is superseded by its final anyway), pressed, marked or acknowledged.
 * Audio is never kept, and a socket in server STT mode does not reconnect at all: the backend
 * expects its frames contiguous from where it left off, which a restarted backend no longer knows.
 */
const KEPT_WHILE_OFFLINE: ReadonlySet<ClientEvent["type"]> = new Set([
  "transcript.client.final",
  "button",
  "marker",
  "ack",
]);

/** Where the socket is: its first connection, a session running, an outage, or the end. */
type Phase = "connecting" | "live" | "reconnecting" | "closed";

/**
 * One session's socket. Creating it asks for `hello` at once, but a browser socket throws
 * `InvalidStateError` on a `send()` while it is still connecting (#298), so every frame asked for
 * before `open` waits in a queue that is flushed, in order, when the connection opens: `hello` is
 * therefore the first frame the backend reads. A socket that ends before opening drops the queue
 * and reports the failure through `handshake`. `handshake` says how the negotiation ended and the accessors
 * keep what `hello.ack` fixed; `onEvent` reports the session from then on, and `close()` ends it.
 *
 * With `reconnect` (#411), a session that was running in client STT mode survives a close that
 * is not final (`FINAL_CLOSE_CODES`): the socket reports `reconnecting` once, then, after each
 * backoff delay, asks `resume` and dials again with a new `hello`, until a `hello.ack` answers
 * (`reconnected`, after the frames the outage kept went out in order) or the session turns out to
 * be over (`closed` with `SESSION_NOT_ACTIVE_CLOSE`). The backend drops a final whose
 * `segment_id` it already handled, so a frame sent twice around the drop is harmless.
 */
export class SessionSocket {
  /** The absolute URL this socket dials. */
  readonly url: string;
  /** How the first negotiation ended. It always resolves: a failure is an answer, not a throw. */
  readonly handshake: Promise<HandshakeResult>;

  private socket!: WebSocket;
  private readonly onEvent: ((event: SessionSocketEvent) => void) | undefined;
  private readonly capabilities: ClientCapabilities;
  private readonly reconnectOptions: ReconnectOptions | null;
  /** Frames asked for while the current connection was connecting, sent in order on `open`. */
  private pending: Array<string | Uint8Array<ArrayBuffer>> = [];
  /** The frames an outage keeps, sent after the resume's `hello.ack`. */
  private readonly offline: ClientEvent[] = [];
  private ack: HelloAck | null = null;
  private version: string | null = null;
  /** Whether the current connection's `hello.ack` arrived. */
  private acknowledged = false;
  private phase: Phase = "connecting";
  /** True once this side ended the socket or refused the backend: no reconnect after that. */
  private final = false;
  private attempts = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private settled = false;
  private resolveHandshake!: (result: HandshakeResult) => void;

  constructor(options: SessionSocketOptions) {
    this.url = socketUrl(options.wsPath);
    this.onEvent = options.onEvent;
    this.capabilities = options.capabilities ?? CAPTURE_CAPABILITIES;
    this.reconnectOptions = options.reconnect ?? null;
    this.handshake = new Promise<HandshakeResult>((resolve) => {
      this.resolveHandshake = resolve;
    });
    this.dial(options.clientTimeMs);
  }

  /** The version both sides speak, or null until `hello.ack` arrived. */
  get protocolVersion(): string | null {
    return this.version;
  }

  /** The STT mode the backend chose, which decides the transcriber the page runs. */
  get sttMode(): HelloAck["stt_mode"] | null {
    return this.ack?.stt_mode ?? null;
  }

  /**
   * Backend clock minus client clock, possibly negative: adding it to a client time gives backend
   * time. Null until `hello.ack` arrived; after a reconnect, the new connection's.
   */
  get clockOffsetMs(): number | null {
    return this.ack?.clock_offset_ms ?? null;
  }

  /** The audio the backend asked for, in server STT mode only; null otherwise. */
  get audioFormat(): AudioFormat | null {
    return this.ack?.audio_format ?? null;
  }

  /** True during an outage, between the drop and the resume's `hello.ack`. */
  get reconnecting(): boolean {
    return this.phase === "reconnecting";
  }

  /** How many frames the current outage keeps for after the resume. */
  get queuedCount(): number {
    return this.offline.length;
  }

  /**
   * Sends one segment of the client's own recognizer, as `transcript.client.partial` or
   * `transcript.client.final`.
   */
  sendTranscript(segment: ClientSegment, kind: TranscriptKind): void {
    const message: TranscriptClientPartial | TranscriptClientFinal =
      kind === "final"
        ? { ...segment, type: "transcript.client.final" }
        : { ...segment, type: "transcript.client.partial" };
    this.send(message);
  }

  /** The student pressed a button: the button equivalent of a voice command (ADR-0006). */
  sendButton(button: ButtonName, clientTimeMs: number, source?: SourceKind): void {
    const message: Button = { type: "button", button, client_time_ms: clientTimeMs };
    if (source !== undefined) message.source = source;
    this.send(message);
  }

  /** The client received the server `command` with that id and acted on it. */
  sendAck(commandId: string, clientTimeMs: number): void {
    this.send({ type: "ack", command_id: commandId, client_time_ms: clientTimeMs });
  }

  /**
   * Sends one encoded audio frame in the layout protocol/README.md defines (`audioFrames.ts`
   * builds them), which only the backend's server STT mode uses.
   */
  sendAudio(frame: Uint8Array<ArrayBuffer>): void {
    if (this.phase === "reconnecting") return;
    this.transmit(frame);
  }

  /**
   * Ends the session socket, and any reconnect in progress. Every send after it is dropped
   * instead of throwing, so a recognizer or a worklet that outlives the session cannot break the
   * page.
   */
  close(): void {
    this.final = true;
    this.clearRetry();
    this.offline.length = 0;
    if (this.phase === "reconnecting") this.phase = "closed";
    this.shut(NORMAL_CLOSURE);
  }

  /** Opens a connection and asks for its `hello`, which waits in `pending` until `open`. */
  private dial(clientTimeMs: number): void {
    const socket = new WebSocket(this.url);
    // v1 server events are all text, so bytes can only be a mistake; asking for an ArrayBuffer
    // keeps that mistake a synchronous report instead of a Blob nobody reads.
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    this.pending = [];
    this.acknowledged = false;
    socket.addEventListener("open", () => {
      if (socket === this.socket) this.onOpen();
    });
    socket.addEventListener("message", (event) => {
      if (socket === this.socket) this.onMessage(event);
    });
    socket.addEventListener("error", (event) => {
      if (socket === this.socket) this.onFailure(event);
    });
    socket.addEventListener("close", (event) => {
      if (socket === this.socket) this.onClosed(event);
    });
    const hello: ClientEvent = {
      type: "hello",
      protocol_version: PROTOCOL_VERSION,
      capabilities: this.capabilities,
      client_time_ms: clientTimeMs,
    };
    this.transmit(JSON.stringify(hello));
  }

  private onOpen(): void {
    for (const frame of this.pending.splice(0)) this.transmit(frame);
  }

  private onMessage(event: MessageEvent): void {
    const frame: unknown = event.data;
    if (typeof frame !== "string") {
      this.refuse("a binary frame is not a server event of protocol v1");
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(frame);
    } catch {
      this.refuse(`the backend sent something that is not JSON: ${preview(frame)}`);
      return;
    }
    let decoded: ServerEvent;
    try {
      decoded = parseServerEvent(parsed);
    } catch (problem) {
      if (!(problem instanceof ProtocolDecodeError)) throw problem;
      this.refuse(problem.message);
      return;
    }
    if (decoded.type === "hello.ack") {
      this.negotiate(decoded);
      return;
    }
    if (!this.acknowledged) {
      this.refuse(`the backend sent ${decoded.type} before hello.ack`);
      return;
    }
    switch (decoded.type) {
      case "transcript.partial":
      case "transcript.final":
        this.notify({ kind: "transcript", event: decoded });
        break;
      case "command":
        this.notify({ kind: "command", event: decoded });
        break;
      case "notice":
        this.notify({ kind: "notice", event: decoded });
        break;
      case "stt.status":
        this.notify({ kind: "sttStatus", event: decoded });
        break;
      case "ack":
        this.notify({ kind: "ack", event: decoded });
        break;
    }
  }

  private negotiate(ack: HelloAck): void {
    let version: string;
    try {
      version = negotiate(ack.protocol_version);
    } catch (problem) {
      if (!(problem instanceof IncompatibleProtocolVersionError)) throw problem;
      this.refuse(problem.message);
      return;
    }
    this.version = version;
    this.ack = ack;
    this.acknowledged = true;
    if (this.phase === "reconnecting") {
      this.phase = "live";
      this.attempts = 0;
      for (const message of this.offline.splice(0)) this.transmit(JSON.stringify(message));
      this.notify({ kind: "reconnected", ack });
      return;
    }
    this.phase = "live";
    this.settle({ kind: "ok", ack, protocolVersion: version });
  }

  /** Whether a close of the running session is an outage to ride out rather than the end. */
  private mayReconnect(): boolean {
    return (
      this.reconnectOptions !== null &&
      !this.final &&
      (this.phase === "live" || this.phase === "reconnecting") &&
      this.ack?.stt_mode === "client"
    );
  }

  private onFailure(event: Event): void {
    this.pending.length = 0;
    // An outage reports itself once, from the close that always follows a failure.
    if (this.mayReconnect()) return;
    const problem =
      event instanceof ErrorEvent && event.message !== ""
        ? event.message
        : "the session socket failed";
    this.settle({ kind: "disconnected", problem });
    this.notify({ kind: "failed", problem });
  }

  private onClosed(event: CloseEvent): void {
    this.pending.length = 0;
    if (this.mayReconnect() && !FINAL_CLOSE_CODES.has(event.code)) {
      this.outage();
      return;
    }
    this.phase = "closed";
    this.offline.length = 0;
    this.clearRetry();
    this.settle({
      kind: "disconnected",
      problem: `the session socket closed with code ${event.code}`,
    });
    this.notify({
      kind: "closed",
      code: event.code,
      reason: event.reason,
      wasClean: event.wasClean,
    });
  }

  /** The connection dropped, or an attempt failed: report the outage once and try again later. */
  private outage(): void {
    if (this.phase !== "reconnecting") {
      this.phase = "reconnecting";
      this.notify({ kind: "reconnecting" });
    }
    const delays =
      this.reconnectOptions?.delaysMs !== undefined && this.reconnectOptions.delaysMs.length > 0
        ? this.reconnectOptions.delaysMs
        : RECONNECT_DELAYS_MS;
    const delay = delays[Math.min(this.attempts, delays.length - 1)];
    this.attempts += 1;
    this.clearRetry();
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.attempt();
    }, delay);
  }

  private async attempt(): Promise<void> {
    const options = this.reconnectOptions;
    if (this.phase !== "reconnecting" || options === null) return;
    let outcome: ResumeOutcome = { kind: "ok" };
    if (options.resume !== undefined) {
      try {
        outcome = await options.resume();
      } catch {
        outcome = { kind: "retry" };
      }
    }
    if (this.phase !== "reconnecting") return;
    if (outcome.kind === "retry") {
      this.outage();
      return;
    }
    if (outcome.kind === "ended") {
      this.phase = "closed";
      this.offline.length = 0;
      this.notify({
        kind: "closed",
        code: SESSION_NOT_ACTIVE_CLOSE,
        reason: outcome.detail ?? "",
        wasClean: true,
      });
      return;
    }
    this.dial((options.clock ?? Date.now)());
  }

  private clearRetry(): void {
    if (this.retryTimer === null) return;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  /** A frame this side will not use: reported always, and fatal while the handshake is pending. */
  private refuse(problem: string): void {
    this.notify({ kind: "rejected", problem });
    if (this.acknowledged) return;
    this.final = true;
    this.settle({ kind: "rejected", problem });
    this.shut(PROTOCOL_ERROR);
  }

  private notify(event: SessionSocketEvent): void {
    this.onEvent?.(event);
  }

  private settle(result: HandshakeResult): void {
    if (this.settled) return;
    this.settled = true;
    this.resolveHandshake(result);
  }

  /**
   * A frame goes out on the connection, unless the session is riding out an outage (or the
   * connection is already going down and one may follow): then the messages worth keeping wait
   * for the resume, bounded, and the rest are dropped.
   */
  private send(message: ClientEvent): void {
    const goingDown =
      this.phase === "live" && this.socket.readyState > this.socket.OPEN && this.mayReconnect();
    if (this.phase === "reconnecting" || goingDown) {
      this.keep(message);
      return;
    }
    this.transmit(JSON.stringify(message));
  }

  private keep(message: ClientEvent): void {
    if (!KEPT_WHILE_OFFLINE.has(message.type)) return;
    this.offline.push(message);
    const bound = this.reconnectOptions?.maxQueued ?? MAX_QUEUED_FRAMES;
    if (this.offline.length > bound) this.offline.splice(0, this.offline.length - bound);
  }

  /**
   * A frame goes out while the socket is open, waits in `pending` while it connects, and is
   * dropped once the socket is closing or closed.
   */
  private transmit(frame: string | Uint8Array<ArrayBuffer>): void {
    const { readyState } = this.socket;
    if (readyState === this.socket.OPEN) this.socket.send(frame);
    else if (readyState === this.socket.CONNECTING) this.pending.push(frame);
  }

  private shut(code: number): void {
    this.pending.length = 0;
    if (this.socket.readyState === this.socket.CLOSED) return;
    this.socket.close(code);
  }
}

/** The head of a frame that is not protocol v1, for the log the `rejected` problem ends up in. */
function preview(frame: string): string {
  return frame.length <= 200 ? frame : `${frame.slice(0, 200)}...`;
}
