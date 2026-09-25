/**
 * A fake `WebSocket` for the capture session (#40). It records every text and binary frame the
 * code under test sends, and a test pushes what the backend would answer with `serverOpen`,
 * `serverMessage`, `serverClose` and `serverError`. jsdom's own `WebSocket` would dial a real
 * server, so a capture test never uses it.
 */

import { FakeEventTarget, swapGlobal } from "./support";

/** One frame the code under test sent: protocol JSON as text, audio as bytes. */
export type SentMessage =
  | { readonly kind: "text"; readonly text: string }
  | { readonly kind: "binary"; readonly bytes: Uint8Array };

let activeSockets: FakeWebSocket[] = [];

export class FakeWebSocket extends FakeEventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;

  readonly url: string;
  /** The sub-protocol the code under test asked for, if any. */
  readonly requestedProtocols: string | string[] | undefined;
  binaryType: BinaryType = "blob";
  readyState: number = FakeWebSocket.CONNECTING;
  /** Every frame sent, in order, including the ones buffered while connecting. */
  readonly sent: SentMessage[] = [];
  /** Every `close()` the code under test called, in order. */
  readonly closeCalls: Array<{ code: number | undefined; reason: string | undefined }> = [];
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  private readonly buffered: SentMessage[] = [];
  private selectedProtocol = "";

  constructor(url: string | URL, protocols?: string | string[]) {
    super();
    this.url = String(url);
    this.requestedProtocols = protocols;
    activeSockets.push(this);
  }

  /** The sub-protocol the server picked in `serverOpen`. */
  get protocol(): string {
    return this.selectedProtocol;
  }

  get sentText(): string[] {
    const texts: string[] = [];
    for (const message of this.sent) if (message.kind === "text") texts.push(message.text);
    return texts;
  }

  get sentBinary(): Uint8Array[] {
    const frames: Uint8Array[] = [];
    for (const message of this.sent) if (message.kind === "binary") frames.push(message.bytes);
    return frames;
  }

  send(data: string | ArrayBuffer | ArrayBufferView | Blob): void {
    if (this.readyState >= FakeWebSocket.CLOSING) {
      throw new DOMException("FakeWebSocket: the socket is already closing or closed", "InvalidStateError");
    }
    const message = toSentMessage(data);
    // A real socket buffers what it is given before `open` and sends it once the handshake ends.
    if (this.readyState === FakeWebSocket.CONNECTING) this.buffered.push(message);
    else this.sent.push(message);
  }

  /**
   * The code under test closing its end. The fake completes the handshake at once -- a test that
   * wants the server's close frame drives `serverClose` instead.
   */
  close(code?: number, reason?: string): void {
    this.closeCalls.push({ code, reason });
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.dispatchClose(code ?? 1000, reason ?? "", true);
  }

  /** Test control: the backend accepted the connection. */
  serverOpen(protocol = ""): void {
    if (this.readyState !== FakeWebSocket.CONNECTING) return;
    this.selectedProtocol = protocol;
    this.readyState = FakeWebSocket.OPEN;
    this.sent.push(...this.buffered.splice(0));
    this.emit("open");
  }

  /** Test control: the backend sent a frame; bytes arrive as the `binaryType` the code asked for. */
  serverMessage(data: string | Uint8Array | ArrayBuffer): void {
    if (this.readyState !== FakeWebSocket.OPEN) {
      throw new Error("FakeWebSocket: push server messages only while the socket is open");
    }
    const payload =
      typeof data === "string"
        ? data
        : this.binaryType === "arraybuffer"
          ? toArrayBuffer(data)
          : new Blob([toArrayBuffer(data)]);
    this.dispatch(new MessageEvent("message", { data: payload }));
  }

  /** Test control: the backend closed the connection (1006 is a lost connection, not a clean end). */
  serverClose(code = 1006, reason = ""): void {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.dispatchClose(code, reason, code !== 1006);
  }

  /** Test control: the connection failed. */
  serverError(message = "fake WebSocket error"): void {
    this.dispatch(new ErrorEvent("error", { message }));
  }

  private dispatchClose(code: number, reason: string, wasClean: boolean): void {
    this.dispatch(new CloseEvent("close", { code, reason, wasClean }));
  }
}

function toSentMessage(data: string | ArrayBuffer | ArrayBufferView | Blob): SentMessage {
  if (typeof data === "string") return { kind: "text", text: data };
  if (data instanceof ArrayBuffer) return { kind: "binary", bytes: new Uint8Array(data.slice(0)) };
  if (ArrayBuffer.isView(data)) {
    const { buffer, byteOffset, byteLength } = data;
    return { kind: "binary", bytes: new Uint8Array(buffer.slice(byteOffset, byteOffset + byteLength)) };
  }
  throw new Error(
    `FakeWebSocket: cannot record a ${data instanceof Blob ? "Blob" : typeof data} frame; the capture client sends text and bytes only`,
  );
}

function toArrayBuffer(data: Uint8Array | ArrayBuffer): ArrayBuffer {
  return data instanceof ArrayBuffer ? data.slice(0) : data.slice().buffer;
}

export interface SocketFakes {
  /** Every socket the code under test opened, in order. */
  readonly sockets: FakeWebSocket[];
  restore(): void;
}

/** Replaces the global `WebSocket` with the fake; the returned `restore()` puts jsdom's back. */
export function installWebSocketFake(): SocketFakes {
  activeSockets = [];
  const restore = swapGlobal("WebSocket", FakeWebSocket);
  return { sockets: activeSockets, restore };
}
