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
  /** The backend stored audio up to `audio_seq` and/or the listed captures. */
  | { kind: "ack"; event: ServerAck }
  /**
   * The socket ended. `wasClean` false (code 1006) is a lost backend connection; a clean close
   * after the page's own `close()` is the normal end of a session.
   */
  | { kind: "closed"; code: number; reason: string; wasClean: boolean }
  /**
   * The connection failed. A browser reports the `closed` event right after this one, so a lost
   * connection reaches the page twice and the message it shows has to cover either.
   */
  | { kind: "failed"; problem: string }
  /** A frame that is not a server event of protocol v1: never used, always reported. */
  | { kind: "rejected"; problem: string };

/** How the `hello` / `hello.ack` negotiation ended; the page shows a message for anything but ok. */
export type HandshakeResult =
  | { kind: "ok"; ack: HelloAck; protocolVersion: string }
  /** The backend speaks another MAJOR version, or its first frame was no usable `hello.ack`. */
  | { kind: "rejected"; problem: string }
  /** The socket ended before the backend answered `hello`. */
  | { kind: "disconnected"; problem: string };

export interface SessionSocketOptions {
  /** The `ws_path` the session start/resume answer returned. */
  wsPath: string;
  /** The client clock reading `hello.client_time_ms` carries; the offset is measured against it. */
  clientTimeMs: number;
  /** What this client can do; the capture page's own capabilities by default. */
  capabilities?: ClientCapabilities;
  /** Called with every decoded server event and every change of the connection itself. */
  onEvent?: (event: SessionSocketEvent) => void;
}

/**
 * One session's socket. Creating it sends `hello` at once: the browser buffers what a connecting
 * socket is given and flushes it in order once the connection opens, which is what makes `hello`
 * the first frame the backend reads. `handshake` says how the negotiation ended and the accessors
 * keep what `hello.ack` fixed; `onEvent` reports the session from then on, and `close()` ends it.
 */
export class SessionSocket {
  /** The absolute URL this socket dialed. */
  readonly url: string;
  /** How the negotiation ended. It always resolves: a failure is an answer, not a throw. */
  readonly handshake: Promise<HandshakeResult>;

  private readonly socket: WebSocket;
  private readonly onEvent: ((event: SessionSocketEvent) => void) | undefined;
  private ack: HelloAck | null = null;
  private version: string | null = null;
  private settled = false;
  private resolveHandshake!: (result: HandshakeResult) => void;

  constructor(options: SessionSocketOptions) {
    this.url = socketUrl(options.wsPath);
    this.onEvent = options.onEvent;
    this.handshake = new Promise<HandshakeResult>((resolve) => {
      this.resolveHandshake = resolve;
    });
    this.socket = new WebSocket(this.url);
    // v1 server events are all text, so bytes can only be a mistake; asking for an ArrayBuffer
    // keeps that mistake a synchronous report instead of a Blob nobody reads.
    this.socket.binaryType = "arraybuffer";
    this.socket.addEventListener("message", (event) => this.onMessage(event));
    this.socket.addEventListener("error", (event) => this.onFailure(event));
    this.socket.addEventListener("close", (event) => this.onClosed(event));
    this.send({
      type: "hello",
      protocol_version: PROTOCOL_VERSION,
      capabilities: options.capabilities ?? CAPTURE_CAPABILITIES,
      client_time_ms: options.clientTimeMs,
    });
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
   * time. Null until `hello.ack` arrived.
   */
  get clockOffsetMs(): number | null {
    return this.ack?.clock_offset_ms ?? null;
  }

  /** The audio the backend asked for, in server STT mode only; null otherwise. */
  get audioFormat(): AudioFormat | null {
    return this.ack?.audio_format ?? null;
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
    if (!this.canSend()) return;
    this.socket.send(frame);
  }

  /**
   * Ends the session socket. Every send after it is dropped instead of throwing, so a recognizer
   * or a worklet that outlives the session cannot break the page.
   */
  close(): void {
    this.shut(NORMAL_CLOSURE);
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
    if (this.ack === null) {
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
      case "ack":
        this.notify({ kind: "ack", event: decoded });
        break;
    }
  }

  private negotiate(ack: HelloAck): void {
    try {
      this.version = negotiate(ack.protocol_version);
    } catch (problem) {
      if (!(problem instanceof IncompatibleProtocolVersionError)) throw problem;
      this.refuse(problem.message);
      return;
    }
    this.ack = ack;
    this.settle({ kind: "ok", ack, protocolVersion: this.version });
  }

  private onFailure(event: Event): void {
    const problem =
      event instanceof ErrorEvent && event.message !== ""
        ? event.message
        : "the session socket failed";
    this.settle({ kind: "disconnected", problem });
    this.notify({ kind: "failed", problem });
  }

  private onClosed(event: CloseEvent): void {
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

  /** A frame this side will not use: reported always, and fatal while the handshake is pending. */
  private refuse(problem: string): void {
    this.notify({ kind: "rejected", problem });
    if (this.ack !== null) return;
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

  /** Frames go out while the socket is connecting (buffered) or open, and never after. */
  private canSend(): boolean {
    const { readyState } = this.socket;
    return readyState === this.socket.CONNECTING || readyState === this.socket.OPEN;
  }

  private send(message: ClientEvent): void {
    if (!this.canSend()) return;
    this.socket.send(JSON.stringify(message));
  }

  private shut(code: number): void {
    if (this.socket.readyState === this.socket.CLOSED) return;
    this.socket.close(code);
  }
}

/** The head of a frame that is not protocol v1, for the log the `rejected` problem ends up in. */
function preview(frame: string): string {
  return frame.length <= 200 ? frame : `${frame.slice(0, 200)}...`;
}
