/**
 * The session socket against the fake `WebSocket` (#40). What the socket sends is checked by
 * decoding it with the bindings' own `parseClientEvent`, and what the backend answers with is the
 * repository's own `protocol/examples/`, so both directions are contract-valid instead of merely
 * matching what this test invented.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type ClientEvent,
  type Command,
  type HelloAck,
  type Notice,
  parseClientEvent,
  PROTOCOL_VERSION,
  type ServerAck,
  type TranscriptFinal,
  type TranscriptPartial,
} from "../protocol";
import { sharedExamples } from "../test/protocolExamples";
import {
  type ClientSegment,
  type HandshakeResult,
  type SessionSocketEvent,
  type SessionSocketOptions,
  type ResumeOutcome,
  SESSION_NOT_ACTIVE_CLOSE,
  SessionSocket,
  socketUrl,
} from "./sessionSocket";
import { type FakeWebSocket, installWebSocketFake } from "./testing";

const WS_PATH = "/ws/sessions/s-20260924-1810";
const CLIENT_TIME_MS = 1790251200000;
/** The head of an audio frame (protocol/README.md): magic, MAJOR, MINOR, `seq` 0, client time. */
const AUDIO_FRAME = new Uint8Array([
  0x53, 0x41, 0x41, 0x46, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0,
]);

const EXAMPLES = Object.fromEntries(sharedExamples().map((entry) => [entry.name, entry.json]));

/** One of the repository's own `protocol/examples/` messages, so the frames are contract-valid. */
function example<T = unknown>(name: string): T {
  const json = EXAMPLES[name];
  if (json === undefined) throw new Error(`there is no shared example named ${name}`);
  return json as T;
}

/** The utterance of the shared examples: an interim partial and the final that replaces it. */
const PARTIAL: ClientSegment = {
  segment_id: "seg-0007",
  client_start_ms: 1790251212300,
  client_end_ms: 1790251213900,
  text: "la derivada de una",
  provider: "web-speech",
  language: "es-ES",
};
const FINAL: ClientSegment = {
  ...PARTIAL,
  client_end_ms: 1790251215100,
  text: "la derivada de una constante es cero",
  confidence: 0.92,
};

interface Harness {
  readonly socket: SessionSocket;
  readonly fake: FakeWebSocket;
  readonly events: SessionSocketEvent[];
}

const restores: Array<() => void> = [];

afterEach(() => {
  while (restores.length > 0) restores.pop()?.();
});

/** Puts the fake socket in place and opens a session socket on it, recording every event. */
function openSession(options: Partial<SessionSocketOptions> = {}): Harness {
  const installed = installWebSocketFake();
  restores.push(installed.restore);
  const events: SessionSocketEvent[] = [];
  const socket = new SessionSocket({
    wsPath: WS_PATH,
    clientTimeMs: CLIENT_TIME_MS,
    onEvent: (event) => events.push(event),
    ...options,
  });
  return { socket, fake: installed.sockets[0], events };
}

/** The backend accepting the connection and answering `hello`, as the flow requires. */
async function answerHello(
  harness: Harness,
  ack: unknown = example("server.hello.ack"),
): Promise<HandshakeResult> {
  harness.fake.serverOpen();
  harness.fake.serverMessage(JSON.stringify(ack));
  return harness.socket.handshake;
}

/** One frame the socket sent, as the JSON object it is. */
function sentJson(fake: FakeWebSocket, index = 0): Record<string, unknown> {
  const text = fake.sentText[index];
  if (text === undefined) throw new Error(`the socket sent no frame ${index}`);
  return JSON.parse(text) as Record<string, unknown>;
}

/** One frame the socket sent, decoded by the bindings: proof it is the message it claims to be. */
function sentEvent(fake: FakeWebSocket, index = 0): ClientEvent {
  return parseClientEvent(sentJson(fake, index));
}

/** Why a handshake did not succeed. */
function problemOf(result: HandshakeResult): string {
  if (result.kind === "ok") throw new Error("the handshake succeeded");
  return result.problem;
}

describe("socketUrl", () => {
  it("dials the ws_path on the page's own origin", () => {
    expect(socketUrl(WS_PATH)).toBe(`ws://${window.location.host}${WS_PATH}`);
  });

  it("dials wss for a page the backend serves over TLS", () => {
    expect(socketUrl(WS_PATH, "https://pc.local/capture")).toBe(`wss://pc.local${WS_PATH}`);
  });

  it("keeps the host of a ws_path that is already absolute", () => {
    expect(socketUrl("ws://127.0.0.1:8000/ws/sessions/s-1", "https://pc.local/capture")).toBe(
      "ws://127.0.0.1:8000/ws/sessions/s-1",
    );
  });
});

describe("hello", () => {
  it("opens the ws_path and buffers hello as the first frame", () => {
    const { fake } = openSession();

    expect(fake.url).toBe(`ws://${window.location.host}${WS_PATH}`);
    expect(fake.binaryType).toBe("arraybuffer");
    expect(fake.sent).toEqual([]);

    fake.serverOpen();

    // The example is of an earlier minor; this client says its own version.
    expect(sentEvent(fake)).toEqual({
      ...example<ClientEvent>("client.hello"),
      protocol_version: PROTOCOL_VERSION,
    });
  });

  it("announces the capabilities the caller gives, without an audio format it did not claim", () => {
    const { fake } = openSession({
      capabilities: { stt: "server", stt_provider: "android-speech" },
    });

    fake.serverOpen();

    expect(sentJson(fake)).toMatchObject({
      protocol_version: PROTOCOL_VERSION,
      client_time_ms: CLIENT_TIME_MS,
    });
    expect(sentJson(fake).capabilities).toEqual({ stt: "server", stt_provider: "android-speech" });
    expect(sentEvent(fake).type).toBe("hello");
  });

  it("keeps hello first when the page sends before the connection opens", () => {
    const { socket, fake } = openSession();

    socket.sendButton("important", CLIENT_TIME_MS + 1000);
    expect(fake.sent).toEqual([]);

    fake.serverOpen();

    expect(sentEvent(fake, 0).type).toBe("hello");
    expect(sentEvent(fake, 1)).toEqual({
      type: "button",
      button: "important",
      client_time_ms: CLIENT_TIME_MS + 1000,
    });
  });
});

describe("before the connection opens (#298)", () => {
  it("sends nothing on a connecting socket, which throws like a browser's, and flushes in order on open", () => {
    const { socket, fake } = openSession();

    socket.sendTranscript(FINAL, "final");
    socket.sendAudio(AUDIO_FRAME);
    expect(fake.sent).toEqual([]);

    fake.serverOpen();

    expect(fake.sent.map((message) => message.kind)).toEqual(["text", "text", "binary"]);
    expect(sentEvent(fake, 0).type).toBe("hello");
    expect(sentEvent(fake, 1).type).toBe("transcript.client.final");
    expect([...fake.sentBinary[0]]).toEqual([...AUDIO_FRAME]);
  });

  it("reports a socket that closes before opening and sends nothing after it", async () => {
    const harness = openSession();
    harness.socket.sendButton("important", CLIENT_TIME_MS);

    harness.fake.serverClose(1006);

    expect(await harness.socket.handshake).toEqual({
      kind: "disconnected",
      problem: "the session socket closed with code 1006",
    });
    expect(harness.events).toEqual([{ kind: "closed", code: 1006, reason: "", wasClean: false }]);
    harness.socket.sendButton("important", CLIENT_TIME_MS);
    expect(harness.fake.sent).toEqual([]);
  });

  it("reports a socket that fails before opening", async () => {
    const harness = openSession();

    harness.fake.serverError("refused");
    harness.fake.serverClose(1006);

    expect(await harness.socket.handshake).toEqual({ kind: "disconnected", problem: "refused" });
    expect(harness.events[0]).toEqual({ kind: "failed", problem: "refused" });
    expect(harness.fake.sent).toEqual([]);
  });

  it("drops the queue when the page closes the socket before it opens", () => {
    const harness = openSession();
    harness.socket.sendButton("important", CLIENT_TIME_MS);

    harness.socket.close();
    harness.fake.serverOpen();

    expect(harness.fake.closeCalls).toEqual([{ code: 1000, reason: undefined }]);
    expect(harness.fake.sent).toEqual([]);
  });
});

describe("the handshake", () => {
  it("keeps the clock offset and the STT mode hello.ack chose", async () => {
    const harness = openSession();
    const ack = example<HelloAck>("server.hello.ack");

    expect(harness.socket.sttMode).toBeNull();
    expect(harness.socket.clockOffsetMs).toBeNull();
    expect(harness.socket.protocolVersion).toBeNull();

    expect(await answerHello(harness)).toEqual({
      kind: "ok",
      ack,
      protocolVersion: PROTOCOL_VERSION,
    });
    expect(harness.socket.sttMode).toBe("client");
    expect(harness.socket.clockOffsetMs).toBe(ack.clock_offset_ms);
    expect(harness.socket.audioFormat).toBeNull();
    expect(harness.socket.protocolVersion).toBe(PROTOCOL_VERSION);
    // The negotiation is the promise's answer, not an event of the session.
    expect(harness.events).toEqual([]);
  });

  it("keeps the audio format the backend asks for in server STT mode", async () => {
    const harness = openSession();
    const ack: HelloAck = {
      ...example<HelloAck>("server.hello.ack"),
      stt_mode: "server",
      audio_format: { encoding: "pcm16", sample_rate_hz: 16000, channels: 1 },
    };

    expect(await answerHello(harness, ack)).toEqual({
      kind: "ok",
      ack,
      protocolVersion: PROTOCOL_VERSION,
    });
    expect(harness.socket.sttMode).toBe("server");
    expect(harness.socket.audioFormat).toEqual({
      encoding: "pcm16",
      sample_rate_hz: 16000,
      channels: 1,
    });
  });

  it("speaks the lower of the two MINOR versions", async () => {
    const harness = openSession();
    const ack: HelloAck = { ...example<HelloAck>("server.hello.ack"), protocol_version: "1.0" };

    expect(await answerHello(harness, ack)).toEqual({ kind: "ok", ack, protocolVersion: "1.0" });
    expect(harness.socket.protocolVersion).toBe("1.0");
  });

  it("refuses a backend that speaks another MAJOR version", async () => {
    const harness = openSession();
    const ack: HelloAck = { ...example<HelloAck>("server.hello.ack"), protocol_version: "2.0" };

    const result = await answerHello(harness, ack);

    expect(result.kind).toBe("rejected");
    expect(problemOf(result)).toContain("incompatible protocol_version 2.0");
    expect(harness.events).toEqual([
      { kind: "rejected", problem: expect.stringContaining("incompatible protocol_version 2.0") },
      { kind: "closed", code: 1002, reason: "", wasClean: true },
    ]);
    expect(harness.fake.closeCalls).toEqual([{ code: 1002, reason: undefined }]);
    expect(harness.socket.sttMode).toBeNull();
  });

  it("refuses a first frame that is not hello.ack", async () => {
    const harness = openSession();

    const result = await answerHello(harness, example("server.transcript.partial"));

    expect(problemOf(result)).toBe("the backend sent transcript.partial before hello.ack");
    expect(harness.fake.closeCalls).toEqual([{ code: 1002, reason: undefined }]);
  });

  it("refuses a first frame of an unknown type", async () => {
    const harness = openSession();
    harness.fake.serverOpen();

    harness.fake.serverMessage('{"type":"nope"}');

    expect(problemOf(await harness.socket.handshake)).toContain(
      "unknown or missing message type",
    );
  });

  it("refuses a first frame that is not JSON, quoting its head", async () => {
    const harness = openSession();
    harness.fake.serverOpen();

    harness.fake.serverMessage("hola, qué tal");

    expect(problemOf(await harness.socket.handshake)).toBe(
      "the backend sent something that is not JSON: hola, qué tal",
    );
  });

  it("refuses a hello.ack missing the clock offset", async () => {
    const harness = openSession();
    const ack: Record<string, unknown> = { ...example<HelloAck>("server.hello.ack") };
    delete ack.clock_offset_ms;

    const result = await answerHello(harness, ack);

    expect(problemOf(result)).toBe("clock_offset_ms: missing required field");
  });

  it("reports a connection the backend never accepted", async () => {
    const harness = openSession();

    harness.fake.serverError();

    const result = await harness.socket.handshake;
    expect(result.kind).toBe("disconnected");
    expect(problemOf(result)).toBe("fake WebSocket error");
    expect(harness.events).toEqual([{ kind: "failed", problem: "fake WebSocket error" }]);
  });

  it("reports a connection lost before hello.ack arrived", async () => {
    const harness = openSession();
    harness.fake.serverOpen();

    harness.fake.serverClose();

    const result = await harness.socket.handshake;
    expect(result.kind).toBe("disconnected");
    expect(problemOf(result)).toBe("the session socket closed with code 1006");
    expect(harness.events).toEqual([
      { kind: "closed", code: 1006, reason: "", wasClean: false },
    ]);
  });
});

describe("server events", () => {
  it("dispatches the backend's normalised transcript", async () => {
    const harness = openSession();
    await answerHello(harness);

    harness.fake.serverMessage(JSON.stringify(example("server.transcript.partial")));
    harness.fake.serverMessage(JSON.stringify(example("server.transcript.final")));

    expect(harness.events).toEqual([
      { kind: "transcript", event: example<TranscriptPartial>("server.transcript.partial") },
      { kind: "transcript", event: example<TranscriptFinal>("server.transcript.final") },
    ]);
  });

  it("dispatches a command, a notice and the backend's own ack", async () => {
    const harness = openSession();
    await answerHello(harness);

    for (const name of ["server.command", "server.notice", "server.ack"]) {
      harness.fake.serverMessage(JSON.stringify(example(name)));
    }

    expect(harness.events).toEqual([
      { kind: "command", event: example<Command>("server.command") },
      { kind: "notice", event: example<Notice>("server.notice") },
      { kind: "ack", event: example<ServerAck>("server.ack") },
    ]);
  });

  it("reports a frame of the wrong shape and keeps the session running", async () => {
    const harness = openSession();
    await answerHello(harness);

    harness.fake.serverMessage(
      JSON.stringify({ ...example<Notice>("server.notice"), pending_count: "dos" }),
    );
    harness.fake.serverMessage(JSON.stringify(example("server.notice")));

    expect(harness.events).toEqual([
      { kind: "rejected", problem: "pending_count: expected an integer" },
      { kind: "notice", event: example<Notice>("server.notice") },
    ]);
    expect(harness.fake.closeCalls).toEqual([]);
  });

  it("reports a binary frame, which a v1 backend never sends", async () => {
    const harness = openSession();
    await answerHello(harness);

    harness.fake.serverMessage(AUDIO_FRAME);

    expect(harness.events).toEqual([
      { kind: "rejected", problem: "a binary frame is not a server event of protocol v1" },
    ]);
  });
});

describe("client events", () => {
  it("sends the recognizer's partial and final as protocol v1 messages", async () => {
    const harness = openSession();
    await answerHello(harness);

    harness.socket.sendTranscript(PARTIAL, "partial");
    harness.socket.sendTranscript(FINAL, "final");

    expect(sentJson(harness.fake, 1)).toEqual(example("client.transcript.client.partial"));
    expect(sentJson(harness.fake, 2)).toEqual(example("client.transcript.client.final"));
    expect(sentEvent(harness.fake, 1).type).toBe("transcript.client.partial");
    expect(sentEvent(harness.fake, 2).type).toBe("transcript.client.final");
    // A segment the recognizer scored sends its score; one it did not sends no confidence at all.
    expect(Object.keys(sentJson(harness.fake, 1))).not.toContain("confidence");
    expect(sentJson(harness.fake, 2).confidence).toBe(0.92);
  });

  it("sends a button, with the source only when it switches", async () => {
    const harness = openSession();
    await answerHello(harness);

    harness.socket.sendButton("switch_source", 1790251230000, "book");
    harness.socket.sendButton("important", 1790251240000);

    expect(sentJson(harness.fake, 1)).toEqual(example("client.button"));
    expect(sentJson(harness.fake, 2)).toEqual({
      type: "button",
      button: "important",
      client_time_ms: 1790251240000,
    });
    expect(Object.keys(sentJson(harness.fake, 2))).not.toContain("source");
  });

  it("answers a command with the client's ack", async () => {
    const harness = openSession();
    await answerHello(harness);

    harness.socket.sendAck("cmd-0003", 1790251260200);

    expect(sentJson(harness.fake, 1)).toEqual(example("client.ack"));
    expect(sentEvent(harness.fake, 1).type).toBe("ack");
  });

  it("sends an audio frame as the very bytes it was given, after the text frames", async () => {
    const harness = openSession();
    await answerHello(harness);

    harness.socket.sendAudio(AUDIO_FRAME);

    expect(harness.fake.sentBinary).toHaveLength(1);
    expect([...harness.fake.sentBinary[0]]).toEqual([...AUDIO_FRAME]);
    expect(harness.fake.sent.map((message) => message.kind)).toEqual(["text", "binary"]);
  });

  it("ends the session with a clean close and drops whatever is sent after it", async () => {
    const harness = openSession();
    await answerHello(harness);

    harness.socket.close();

    expect(harness.fake.closeCalls).toEqual([{ code: 1000, reason: undefined }]);
    expect(harness.events).toEqual([
      { kind: "closed", code: 1000, reason: "", wasClean: true },
    ]);

    harness.socket.sendButton("important", CLIENT_TIME_MS);
    harness.socket.sendTranscript(FINAL, "final");
    harness.socket.sendAudio(AUDIO_FRAME);
    harness.socket.close();

    expect(harness.fake.sentText).toHaveLength(1);
    expect(harness.fake.sentBinary).toEqual([]);
    expect(harness.fake.closeCalls).toHaveLength(1);
  });
});

describe("reconnect (#411)", () => {
  interface Reconnecting {
    readonly socket: SessionSocket;
    readonly sockets: FakeWebSocket[];
    readonly events: SessionSocketEvent[];
    readonly resumes: number[];
  }

  afterEach(() => {
    vi.useRealTimers();
  });

  /** A running client-mode session whose socket reconnects, asking `resume` before each attempt. */
  async function running(
    resume: () => Promise<ResumeOutcome> = async () => ({ kind: "ok" }),
    options: { maxQueued?: number; ack?: unknown } = {},
  ): Promise<Reconnecting> {
    vi.useFakeTimers();
    const installed = installWebSocketFake();
    restores.push(installed.restore);
    const events: SessionSocketEvent[] = [];
    const resumes: number[] = [];
    let clock = CLIENT_TIME_MS;
    const socket = new SessionSocket({
      wsPath: WS_PATH,
      clientTimeMs: CLIENT_TIME_MS,
      onEvent: (event) => events.push(event),
      reconnect: {
        resume: () => {
          resumes.push(Date.now());
          return resume();
        },
        delaysMs: [1000, 2000, 5000],
        clock: () => (clock += 1000),
        maxQueued: options.maxQueued,
      },
    });
    installed.sockets[0].serverOpen();
    installed.sockets[0].serverMessage(JSON.stringify(options.ack ?? example("server.hello.ack")));
    expect((await socket.handshake).kind).toBe("ok");
    return { socket, sockets: installed.sockets, events, resumes };
  }

  function types(fake: FakeWebSocket): string[] {
    return fake.sentText.map((text) => (JSON.parse(text) as { type: string }).type);
  }

  it("rides out a dropped connection: resumes, says hello again and flushes what was said meanwhile", async () => {
    const session = await running();

    session.sockets[0].serverClose(1006);
    expect(session.events).toEqual([{ kind: "reconnecting" }]);
    expect(session.socket.reconnecting).toBe(true);

    session.socket.sendTranscript(PARTIAL, "partial");
    session.socket.sendTranscript(FINAL, "final");
    session.socket.sendTranscript({ ...FINAL, segment_id: "seg-0008", text: "y la de x es uno" }, "final");
    session.socket.sendAudio(AUDIO_FRAME);
    expect(session.socket.queuedCount).toBe(2);

    await vi.advanceTimersByTimeAsync(999);
    expect(session.sockets).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(session.resumes).toHaveLength(1);
    expect(session.sockets).toHaveLength(2);

    const next = session.sockets[1];
    next.serverOpen();
    expect(types(next)).toEqual(["hello"]);
    expect(sentEvent(next, 0)).toMatchObject({ type: "hello", client_time_ms: CLIENT_TIME_MS + 1000 });

    next.serverMessage(JSON.stringify(example("server.hello.ack")));

    expect(types(next)).toEqual(["hello", "transcript.client.final", "transcript.client.final"]);
    expect(sentEvent(next, 1)).toMatchObject({ segment_id: "seg-0007" });
    expect(sentEvent(next, 2)).toMatchObject({ segment_id: "seg-0008" });
    expect(next.sentBinary).toEqual([]);
    expect(session.events.map((event) => event.kind)).toEqual(["reconnecting", "reconnected"]);
    expect(session.socket.reconnecting).toBe(false);
    expect(session.socket.queuedCount).toBe(0);

    // Live again: frames go straight out, and server events reach the page.
    session.socket.sendButton("important", CLIENT_TIME_MS);
    expect(types(next).at(-1)).toBe("button");
    next.serverMessage(JSON.stringify(example("server.notice")));
    expect(session.events.at(-1)?.kind).toBe("notice");
  });

  it("backs off while the backend is not there and starts over after a resume", async () => {
    let answer: ResumeOutcome = { kind: "retry" };
    const session = await running(async () => answer);

    session.sockets[0].serverClose(1001);
    await vi.advanceTimersByTimeAsync(1000);
    expect(session.resumes).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1999);
    expect(session.resumes).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(session.resumes).toHaveLength(2);
    expect(session.sockets).toHaveLength(1);

    answer = { kind: "ok" };
    await vi.advanceTimersByTimeAsync(5000);
    expect(session.sockets).toHaveLength(2);
    // An attempt whose connection fails is one more outage step, reported only once.
    session.sockets[1].serverError();
    session.sockets[1].serverClose(1006);
    await vi.advanceTimersByTimeAsync(5000);
    expect(session.sockets).toHaveLength(3);
    session.sockets[2].serverOpen();
    session.sockets[2].serverMessage(JSON.stringify(example("server.hello.ack")));
    expect(session.events.map((event) => event.kind)).toEqual(["reconnecting", "reconnected"]);

    // A second outage waits the first delay again.
    session.sockets[2].serverClose(1006);
    await vi.advanceTimersByTimeAsync(1000);
    expect(session.sockets).toHaveLength(4);
  });

  it("gives up with the session-ended close when the resume says the session is over", async () => {
    const session = await running(async () => ({ kind: "ended", detail: "La sesión ya ha terminado." }));
    session.sockets[0].serverClose(1006);
    session.socket.sendTranscript(FINAL, "final");

    await vi.advanceTimersByTimeAsync(1000);

    expect(session.sockets).toHaveLength(1);
    expect(session.events).toEqual([
      { kind: "reconnecting" },
      { kind: "closed", code: SESSION_NOT_ACTIVE_CLOSE, reason: "La sesión ya ha terminado.", wasClean: true },
    ]);
    expect(session.socket.queuedCount).toBe(0);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(session.sockets).toHaveLength(1);
  });

  it("does not reconnect after a final close code", async () => {
    const session = await running();

    session.sockets[0].serverClose(SESSION_NOT_ACTIVE_CLOSE, "session s-1 is no longer active");
    await vi.advanceTimersByTimeAsync(60_000);

    expect(session.sockets).toHaveLength(1);
    expect(session.events).toEqual([
      { kind: "closed", code: SESSION_NOT_ACTIVE_CLOSE, reason: "session s-1 is no longer active", wasClean: true },
    ]);
  });

  it("gives up when the reconnect is refused as not active", async () => {
    const session = await running();
    session.sockets[0].serverClose(1006);
    await vi.advanceTimersByTimeAsync(1000);

    session.sockets[1].serverOpen();
    session.sockets[1].serverClose(SESSION_NOT_ACTIVE_CLOSE, "ended");
    await vi.advanceTimersByTimeAsync(60_000);

    expect(session.sockets).toHaveLength(2);
    expect(session.events.map((event) => event.kind)).toEqual(["reconnecting", "closed"]);
  });

  it("stops reconnecting when the page closes it", async () => {
    const session = await running();
    session.sockets[0].serverClose(1006);

    session.socket.close();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(session.sockets).toHaveLength(1);
    expect(session.resumes).toEqual([]);
    expect(session.events).toEqual([{ kind: "reconnecting" }]);
  });

  it("keeps a bounded queue, dropping the oldest frames", async () => {
    const session = await running(undefined, { maxQueued: 2 });
    session.sockets[0].serverClose(1006);
    for (const id of ["a", "b", "c"]) {
      session.socket.sendTranscript({ ...FINAL, segment_id: id }, "final");
    }
    expect(session.socket.queuedCount).toBe(2);

    await vi.advanceTimersByTimeAsync(1000);
    session.sockets[1].serverOpen();
    session.sockets[1].serverMessage(JSON.stringify(example("server.hello.ack")));

    expect([1, 2].map((index) => sentJson(session.sockets[1], index).segment_id)).toEqual(["b", "c"]);
  });

  it("does not reconnect a session in server STT mode", async () => {
    const session = await running(undefined, {
      ack: {
        ...example<HelloAck>("server.hello.ack"),
        stt_mode: "server",
        audio_format: { encoding: "pcm16", sample_rate_hz: 16000, channels: 1 },
      },
    });

    session.sockets[0].serverClose(1006);
    await vi.advanceTimersByTimeAsync(60_000);

    expect(session.sockets).toHaveLength(1);
    expect(session.events).toEqual([{ kind: "closed", code: 1006, reason: "", wasClean: false }]);
  });
});
