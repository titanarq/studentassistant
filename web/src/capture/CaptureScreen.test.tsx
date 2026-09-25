/**
 * The capture screen and the page that carries it (#40), driven end to end by the capture fakes: a
 * fake camera and `ImageCapture`, a fake `SpeechRecognition`, a fake `WebSocket`, a fake Web Audio
 * graph and a mocked `fetch`. Nothing here touches a device, a network or a backend.
 *
 * What these tests hold the screen to is what the student sees and what goes on the wire. The wire
 * is read back as the protocol's own messages -- `hello` first, then a `button`, an `ack`, a
 * `transcript.client.*` or a binary audio frame -- and the page as its Spanish: the state of a
 * burst, the transcript, and one sentence per way a camera, a microphone, a browser or a connection
 * can refuse. Every body a route answers is shaped like the `protocol/examples/rest.*` message of
 * the same name, because the screen decodes all of them through the bindings.
 */

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { decodeCaptureUploadRequest, PROTOCOL_VERSION, type Session } from "../protocol";
import CapturePage from "./CapturePage";
import CaptureScreen, { type CaptureScreenProps, captureCapabilities } from "./CaptureScreen";
import type { PcmWorkletChunk } from "./pcmWorklet";
import {
  type CaptureFakes,
  FakeBiasingSpeechRecognition,
  FakeSpeechRecognitionPhrase,
  type FakeWebSocket,
  installCaptureFakes,
  swapGlobal,
  swapProperty,
} from "./testing";

const NOW = 1790251200000;

const SESSION: Session = {
  session_id: "s-20260924-1810",
  subject_id: "biologia",
  topic_id: "fotosintesis",
  status: "active",
  started_at_ms: NOW,
  ws_path: "/ws/sessions/s-20260924-1810",
  protocol_version: PROTOCOL_VERSION,
};

const CAPTURES_PATH = `/api/sessions/${SESSION.session_id}/captures`;
const END_PATH = `/api/sessions/${SESSION.session_id}/end`;
const ENDED = { session_id: SESSION.session_id, status: "ended", ended_at_ms: NOW + 60_000 };

/** The `metadata` part of a burst as the backend reads it back. */
interface SentMetadata {
  capture_id: string;
  trigger: "button" | "command";
  command_id?: string;
  client_time_ms: number;
  images: Array<{
    part: string;
    content_type: string;
    width_px: number;
    height_px: number;
    client_time_ms: number;
  }>;
}

/** One frame the screen put on the socket, parsed back. */
interface SentFrame {
  type: string;
  [key: string]: unknown;
}

let fakes: CaptureFakes;
let shutter: Mock<() => void>;
const sent: Array<{ path: string; init: RequestInit }> = [];
const restores: Array<() => void> = [];
const objectUrls: string[] = [];
const revokedUrls: string[] = [];

type Route = (init: RequestInit) => Response | Promise<Response>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(routes: Record<string, Route>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit = {}) => {
      sent.push({ path, init });
      const route = routes[path];
      return route === undefined
        ? jsonResponse({ detail: `Ninguna ruta de prueba responde a ${path}.` }, 404)
        : await route(init);
    }),
  );
}

/** The answer of a burst the backend took, echoing the `capture_id` the screen minted for it. */
async function storedResponse(
  init: RequestInit,
  status: "stored" | "duplicate" = "stored",
): Promise<Response> {
  const metadata = await metadataOf(init);
  return jsonResponse({
    capture_id: metadata.capture_id,
    session_id: SESSION.session_id,
    status,
    image_count: metadata.images.length,
    received_at_ms: NOW,
  });
}

/** The backend of a normal session: it takes bursts and ends the session when asked. */
function backend(routes: Record<string, Route> = {}): void {
  stubFetch({
    [CAPTURES_PATH]: (init) => storedResponse(init),
    [END_PATH]: () => jsonResponse(ENDED),
    ...routes,
  });
}

async function metadataOf(init: RequestInit): Promise<SentMetadata> {
  const form = init.body as FormData;
  const metadata = form.get("metadata");
  if (!(metadata instanceof Blob)) throw new Error("the burst carried no metadata part");
  return JSON.parse(await metadata.text()) as SentMetadata;
}

function capturePost(): { path: string; init: RequestInit } {
  const post = sent.find((call) => call.path === CAPTURES_PATH);
  if (post === undefined) throw new Error("the screen uploaded no burst");
  return post;
}

/** The session socket the screen opened; there is only ever one per screen. */
function socket(): FakeWebSocket {
  const opened = fakes.sockets.at(-1);
  if (opened === undefined) throw new Error("the screen opened no session socket");
  return opened;
}

function frames(): SentFrame[] {
  return socket().sentText.map((text) => JSON.parse(text) as SentFrame);
}

function lastFrame(): SentFrame {
  const frame = frames().at(-1);
  if (frame === undefined) throw new Error("the screen sent nothing on the socket");
  return frame;
}

/** The `hello.ack` a backend of protocol v1 answers with, in the STT mode a test asks for. */
function helloAck(
  sttMode: "client" | "server" = "client",
  extra: Record<string, unknown> = {},
): string {
  const ack: Record<string, unknown> = {
    type: "hello.ack",
    protocol_version: PROTOCOL_VERSION,
    stt_mode: sttMode,
    clock_offset_ms: 12,
    server_time_ms: NOW + 12,
    ...extra,
  };
  if (sttMode === "server") {
    ack.audio_format = { encoding: "pcm16", sample_rate_hz: 16_000, channels: 1 };
  }
  return JSON.stringify(ack);
}

function renderScreen(props: Partial<CaptureScreenProps> = {}) {
  shutter = vi.fn<() => void>();
  return render(
    <CaptureScreen
      session={SESSION}
      subjectName="Biología"
      topicName="Fotosíntesis"
      now={() => NOW}
      playShutter={shutter}
      {...props}
    />,
  );
}

/**
 * The backend accepted the socket and answered `hello`, and the camera came up: the session is
 * running. A test of a camera that refuses passes `cameraStarts` false and waits for its own
 * Spanish sentence instead.
 */
async function open(
  sttMode: "client" | "server" = "client",
  cameraStarts = true,
  ackExtra: Record<string, unknown> = {},
): Promise<void> {
  await act(async () => {
    socket().serverOpen();
    socket().serverMessage(helloAck(sttMode, ackExtra));
  });
  await waitFor(() =>
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Conectado con el servidor",
    ),
  );
  if (!cameraStarts) return;
  await waitFor(() =>
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
      "La cámara está en marcha.",
    ),
  );
}

/** The backend sending one of its own messages. */
async function push(message: unknown): Promise<void> {
  await act(async () => {
    socket().serverMessage(JSON.stringify(message));
  });
}

function transcript() {
  return within(screen.getByRole("list", { name: "Segmentos transcritos" }));
}

async function capture(): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Capturar" }));
  });
}

beforeEach(() => {
  fakes = installCaptureFakes();
  sent.length = 0;
  objectUrls.length = 0;
  revokedUrls.length = 0;
  backend();
  // jsdom implements neither a media element that plays nor an object URL. The screen previews the
  // camera stream and hangs a thumbnail of every burst on one, and neither is what a test of the
  // screen is about, so both are answered here.
  restores.push(swapProperty(HTMLMediaElement.prototype, "play", () => Promise.resolve()));
  restores.push(
    swapProperty(URL, "createObjectURL", () => {
      const url = `blob:fake/${objectUrls.length}`;
      objectUrls.push(url);
      return url;
    }),
  );
  restores.push(
    swapProperty(URL, "revokeObjectURL", (url: string) => {
      revokedUrls.push(url);
    }),
  );
});

afterEach(() => {
  for (const restore of restores.splice(0).reverse()) restore();
  fakes.restore();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the session socket", () => {
  it("dials the session's ws_path and sends hello first", async () => {
    renderScreen();
    expect(fakes.sockets).toHaveLength(1);
    expect(socket().url).toContain(SESSION.ws_path);
    // v1 server events are text, so a binary frame can only be a mistake the screen reports.
    expect(socket().binaryType).toBe("arraybuffer");

    await open();

    expect(frames()).toEqual([
      {
        type: "hello",
        protocol_version: PROTOCOL_VERSION,
        capabilities: {
          stt: "client",
          stt_provider: "web-speech",
          audio_format: { encoding: "pcm16", sample_rate_hz: 16_000, channels: 1 },
        },
        client_time_ms: NOW,
      },
    ]);
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Transcribe este navegador.",
    );
  });

  it("promises audio only when this browser can stream it", async () => {
    restores.push(swapGlobal("AudioContext", undefined));
    expect(captureCapabilities().audio_format).toBeUndefined();

    renderScreen();
    await open();

    expect(frames()[0].capabilities).toEqual({ stt: "client", stt_provider: "web-speech" });
  });

  it("says in Spanish when the backend speaks another protocol version", async () => {
    renderScreen();
    await act(async () => {
      socket().serverOpen();
      socket().serverMessage(
        JSON.stringify({ ...JSON.parse(helloAck()), protocol_version: "2.0" }),
      );
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no hablan la misma versión del protocolo",
    );
    expect(socket().closeCalls).toEqual([{ code: 1002, reason: undefined }]);
    expect(screen.getByRole("button", { name: "Capturar" })).toBeDisabled();
  });

  it("refuses a frame that is no server event of protocol v1", async () => {
    renderScreen();
    await open();

    await push({ type: "nonsense" });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El servidor ha enviado un mensaje que no sigue el protocolo esperado.",
    );
  });

  it("says in Spanish when the backend connection is lost", async () => {
    renderScreen();
    await open();

    await act(async () => {
      socket().serverClose(1006);
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Se ha perdido la conexión con el servidor.",
    );
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Se ha perdido la conexión con el servidor",
    );
    expect(screen.getByRole("button", { name: "Capturar" })).toBeDisabled();
  });

  it("cannot open a session at all from an address the browser does not trust", async () => {
    restores.push(swapGlobal("isSecureContext", false));

    renderScreen();

    expect(await screen.findByRole("alert")).toHaveTextContent("origen seguro");
    expect(fakes.sockets).toHaveLength(0);
    expect(fakes.devices.getUserMediaCalls).toHaveLength(0);
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Esta página no puede abrir la sesión",
    );
  });
});

describe("the camera", () => {
  it("previews the stream it opened for the page photographs", async () => {
    const { container } = renderScreen();
    await open();

    expect(fakes.devices.getUserMediaCalls).toHaveLength(1);
    // The microphone is the transcriber's to open, so the camera never asks for it.
    expect(fakes.devices.getUserMediaCalls[0].audio).toBe(false);
    const preview = container.querySelector("video");
    expect(preview?.srcObject).toBe(fakes.devices.streams[0]);
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
      "La cámara está en marcha.",
    );
  });

  it("says in Spanish when the student refused the camera", async () => {
    fakes.devices.failure = "NotAllowedError";
    renderScreen();
    await open("client", false);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El navegador ha bloqueado la cámara.",
    );
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
      "La cámara no está en marcha.",
    );
    expect(screen.getByRole("button", { name: "Capturar" })).toBeDisabled();
    // The session itself goes on: a page without a camera still sends what the student says.
    expect(screen.getByRole("button", { name: "Importante" })).toBeEnabled();
  });

  it("says in Spanish when the camera goes away mid-session", async () => {
    renderScreen();
    await open();

    await act(async () => {
      fakes.videoTrack.endFromDevice();
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La cámara se ha desconectado o otra aplicación se la ha quedado.",
    );
    expect(screen.getByRole("button", { name: "Capturar" })).toBeDisabled();
  });
});

describe("the transcriber hello.ack chose", () => {
  it("recognizes in the browser in client mode and sends every utterance", async () => {
    renderScreen();
    await open("client");

    expect(fakes.recognitions).toHaveLength(1);
    const recognition = fakes.recognitions[0];
    expect(recognition.lang).toBe("es-ES");
    expect(recognition.continuous).toBe(true);
    expect(recognition.interimResults).toBe(true);
    expect(recognition.startCount).toBe(1);
    // Client mode streams no audio: the browser did the recognizing itself.
    expect(fakes.contexts).toHaveLength(0);

    await act(async () => {
      recognition.emitResult([{ transcript: "la mitocondria" }]);
    });
    const partial = lastFrame();
    expect(partial).toMatchObject({
      type: "transcript.client.partial",
      text: "la mitocondria",
      provider: "web-speech",
      language: "es-ES",
    });

    await act(async () => {
      recognition.emitResult([{ transcript: "la mitocondria produce energía", final: true }]);
    });
    const final = lastFrame();
    expect(final.type).toBe("transcript.client.final");
    expect(final.text).toBe("la mitocondria produce energía");
    // One utterance is one segment: the final replaces the partials that share its id.
    expect(final.segment_id).toBe(partial.segment_id);
    expect(Number(final.client_start_ms)).toBeLessThanOrEqual(Number(final.client_end_ms));
  });

  it("streams the microphone to the backend in server mode and runs no recognizer", async () => {
    renderScreen();
    await open("server");

    expect(fakes.recognitions).toHaveLength(0);
    expect(fakes.contexts).toHaveLength(1);
    expect(fakes.workletNodes).toHaveLength(1);
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Transcribe el servidor",
    );

    // Half a second of a 48 kHz microphone, as the worklet processor hands it over.
    const chunk: PcmWorkletChunk = {
      samples: Float32Array.from({ length: 24_000 }, () => 0.25),
      startTimeSec: 0,
    };
    await act(async () => {
      fakes.workletNodes[0].emitProcessorMessage(chunk);
    });

    const streamed = socket().sentBinary;
    expect(streamed.length).toBeGreaterThan(0);
    expect(new TextDecoder().decode(streamed[0].slice(0, 4))).toBe("SAAF");
  });

  it("says in Spanish when this browser cannot recognize speech", async () => {
    restores.push(swapGlobal("SpeechRecognition", undefined));
    restores.push(swapGlobal("webkitSpeechRecognition", undefined));
    renderScreen();
    await open();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Este navegador no reconoce el habla por sí mismo.",
    );
    // The camera has nothing to do with the recognizer, so the student can still photograph pages.
    expect(screen.getByRole("button", { name: "Capturar" })).toBeEnabled();
  });
});

describe("vocabulary hints", () => {
  const HINTS = ["Biología", "Fotosíntesis", "clorofila"];

  /** A browser whose recognizer takes phrase hints, in place of the default one without them. */
  function biasingBrowser(): void {
    restores.push(swapGlobal("SpeechRecognition", FakeBiasingSpeechRecognition));
    restores.push(swapGlobal("webkitSpeechRecognition", FakeBiasingSpeechRecognition));
    restores.push(swapGlobal("SpeechRecognitionPhrase", FakeSpeechRecognitionPhrase));
  }

  function startedWith(index: number): string[][] {
    const recognition = fakes.recognitions[index];
    if (!(recognition instanceof FakeBiasingSpeechRecognition)) {
      throw new Error(`recognition ${index} is not a biasing one`);
    }
    return recognition.phrasesAtStart;
  }

  /** The browser ending the running recognition, and the page's transcriber starting the next. */
  async function restartRecognition(): Promise<void> {
    const count = fakes.recognitions.length;
    await act(async () => {
      fakes.recognitions[count - 1].emitEnd();
    });
    await waitFor(() => expect(fakes.recognitions).toHaveLength(count + 1));
  }

  it("biases the recognizer towards the hello.ack hints and the lists notices replace them with", async () => {
    biasingBrowser();
    renderScreen();
    await open("client", true, { vocabulary_hints: HINTS });

    expect(startedWith(0)).toEqual([HINTS]);

    await push({
      type: "notice",
      pending_count: 0,
      server_time_ms: NOW,
      vocabulary_hints: ["Biología", "estoma"],
    });
    await restartRecognition();
    expect(startedWith(1)).toEqual([["Biología", "estoma"]]);

    // A notice without the field keeps the list it has.
    await push({ type: "notice", pending_count: 2, server_time_ms: NOW });
    await restartRecognition();
    expect(startedWith(2)).toEqual([["Biología", "estoma"]]);
  });

  it("starts with the list of a notice that arrived before the recognizer did", async () => {
    biasingBrowser();
    renderScreen();
    await act(async () => {
      socket().serverOpen();
      socket().serverMessage(helloAck("client", { vocabulary_hints: HINTS }));
      socket().serverMessage(
        JSON.stringify({
          type: "notice",
          pending_count: 0,
          server_time_ms: NOW,
          vocabulary_hints: ["estoma"],
        }),
      );
    });
    await waitFor(() => expect(fakes.recognitions).toHaveLength(1));

    expect(startedWith(0)).toEqual([["estoma"]]);
  });

  it("recognizes without the hints, and says nothing, in a browser that cannot take them", async () => {
    renderScreen();
    await open("client", true, { vocabulary_hints: HINTS });
    await push({
      type: "notice",
      pending_count: 0,
      server_time_ms: NOW,
      vocabulary_hints: ["estoma"],
    });

    expect(fakes.recognitions).toHaveLength(1);
    expect("phrases" in fakes.recognitions[0]).toBe(false);
    expect(fakes.recognitions[0].startCount).toBe(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("the student's buttons", () => {
  it("takes a burst of three on Capturar, flashes, clicks and uploads it as one post", async () => {
    renderScreen();
    await open();

    await capture();

    expect(fakes.photos.taken).toHaveLength(3);
    expect(shutter).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("capture-flash")).toBeInTheDocument();

    const post = capturePost();
    const metadata = await metadataOf(post.init);
    // The part the backend decodes first: it has to be a `rest.sessions.captures.request`.
    expect(() => decodeCaptureUploadRequest(metadata, "")).not.toThrow();
    expect(metadata.trigger).toBe("button");
    expect(metadata.command_id).toBeUndefined();
    expect(metadata.capture_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    expect(metadata.images.map((image) => image.part)).toEqual(["image_0", "image_1", "image_2"]);
    expect(metadata.images[0]).toMatchObject({
      content_type: "image/png",
      width_px: 1280,
      height_px: 720,
    });
    const form = post.init.body as FormData;
    expect([...form.keys()].filter((part) => part.startsWith("image_"))).toHaveLength(3);

    // The strip carries the burst's thumbnail and says how its upload is going.
    expect(await screen.findByText("Guardada")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Ráfaga 1" })).toHaveAttribute("src", objectUrls[0]);
    await waitFor(() => expect(screen.queryByTestId("capture-flash")).not.toBeInTheDocument());
  });

  it("counts a burst the backend already had as stored", async () => {
    backend({ [CAPTURES_PATH]: (init) => storedResponse(init, "duplicate") });
    renderScreen();
    await open();

    await capture();

    expect(await screen.findByText("Duplicada")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says in Spanish when the backend refuses a burst", async () => {
    backend({
      [CAPTURES_PATH]: () => jsonResponse({ detail: "La sesión ya ha terminado." }, 409),
    });
    renderScreen();
    await open();

    await capture();

    expect(await screen.findByText("Error")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El servidor no ha guardado las fotografías: La sesión ya ha terminado.",
    );
  });

  it("marks a burst stored when the backend's own ack lists it", async () => {
    // An upload that has not answered yet: only the socket's `ack` can settle it.
    let answered: ((response: Response) => void) | undefined;
    backend({
      [CAPTURES_PATH]: () =>
        new Promise<Response>((resolve) => {
          answered = resolve;
        }),
    });
    renderScreen();
    await open();

    await capture();
    expect(await screen.findByText("Subiendo…")).toBeInTheDocument();

    const metadata = await metadataOf(capturePost().init);
    await push({ type: "ack", capture_ids: [metadata.capture_id], server_time_ms: NOW + 500 });
    expect(await screen.findByText("Guardada")).toBeInTheDocument();

    await act(async () => {
      answered?.(await storedResponse(capturePost().init));
    });
    expect(screen.getByText("Guardada")).toBeInTheDocument();
  });

  it("flags a point of the session on Importante", async () => {
    renderScreen();
    await open();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Importante" }));
    });

    expect(lastFrame()).toEqual({ type: "button", button: "important", client_time_ms: NOW });
  });

  it("says which of the book or the notes is being photographed", async () => {
    renderScreen();
    await open();
    const book = screen.getByRole("button", { name: "Libro" });
    const notes = screen.getByRole("button", { name: "Apuntes" });
    // The backend knows no source until the student names one, so neither starts out selected.
    expect(book).toHaveAttribute("aria-pressed", "false");
    expect(notes).toHaveAttribute("aria-pressed", "false");

    await act(async () => {
      fireEvent.click(book);
    });
    expect(lastFrame()).toEqual({
      type: "button",
      button: "switch_source",
      source: "book",
      client_time_ms: NOW,
    });
    expect(book).toHaveAttribute("aria-pressed", "true");
    expect(notes).toHaveAttribute("aria-pressed", "false");

    await act(async () => {
      fireEvent.click(notes);
    });
    expect(lastFrame()).toEqual({
      type: "button",
      button: "switch_source",
      source: "notes",
      client_time_ms: NOW,
    });
    expect(notes).toHaveAttribute("aria-pressed", "true");
    expect(book).toHaveAttribute("aria-pressed", "false");
  });

  it("ends the session on Terminar and gives every device back", async () => {
    const ended = vi.fn();
    renderScreen({ onEnded: ended });
    await open();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Terminar" }));
    });

    const post = sent.find((call) => call.path === END_PATH);
    if (post === undefined) throw new Error("the screen posted no end of session");
    expect(post.init.method).toBe("POST");
    expect(JSON.parse(post.init.body as string)).toEqual({
      client_time_ms: NOW,
      reason: "button",
    });
    expect(ended).toHaveBeenCalledWith(ENDED);
    expect(socket().closeCalls).toEqual([{ code: 1000, reason: undefined }]);
    expect(fakes.videoTrack.readyState).toBe("ended");
    expect(fakes.recognitions[0].abortCount).toBe(1);
    // This close was the student's own, so it is not a lost connection.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says in Spanish when the backend will not end the session, and stays on the page", async () => {
    backend({ [END_PATH]: () => jsonResponse({ detail: "La sesión ya está terminada." }, 409) });
    renderScreen();
    await open();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Terminar" }));
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No se ha podido terminar la sesión: La sesión ya está terminada.",
    );
    // The devices are given back either way, but the student can still ask the backend to end it.
    expect(screen.getByRole("button", { name: "Terminar" })).toBeEnabled();
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
      "La cámara no está en marcha.",
    );
  });

  it("gives the thumbnails of the strip back when the screen goes away", async () => {
    const { unmount } = renderScreen();
    await open();

    await capture();
    await screen.findByText("Guardada");
    expect(objectUrls).toHaveLength(1);

    unmount();
    expect(revokedUrls).toEqual(objectUrls);
  });
});

describe("what the backend sends back", () => {
  it("shows a partial in grey and replaces it with the final in black", async () => {
    renderScreen();
    await open();
    const segment = {
      segment_id: "seg-1",
      session_start_ms: 1000,
      session_end_ms: 2500,
      language: "es",
    };

    await push({ type: "transcript.partial", ...segment, text: "la célul" });
    expect(transcript().getAllByRole("listitem")).toHaveLength(1);
    expect(transcript().getByText("la célul")).toHaveStyle("color: rgb(92, 92, 92)");

    await push({ type: "transcript.final", ...segment, text: "la célula" });
    // The final settles the utterance: one item, the interim text gone.
    expect(transcript().getAllByRole("listitem")).toHaveLength(1);
    expect(transcript().getByText("la célula")).toHaveStyle("color: rgb(17, 17, 17)");
    expect(transcript().queryByText("la célul")).not.toBeInTheDocument();
  });

  it("keeps two utterances apart and counts the doubts the backend reports", async () => {
    renderScreen();
    await open();
    expect(screen.getByRole("status", { name: "Dudas pendientes" })).toHaveTextContent(
      "El servidor todavía no ha avisado de ninguna duda.",
    );

    await push({
      type: "transcript.final",
      segment_id: "seg-1",
      session_start_ms: 0,
      session_end_ms: 900,
      text: "Primera frase.",
      language: "es",
    });
    await push({
      type: "transcript.partial",
      segment_id: "seg-2",
      session_start_ms: 900,
      session_end_ms: 1200,
      text: "Segunda",
      language: "es",
    });
    expect(transcript().getAllByRole("listitem")).toHaveLength(2);

    await push({ type: "notice", pending_count: 3, server_time_ms: NOW });
    expect(screen.getByRole("status", { name: "Dudas pendientes" })).toHaveTextContent(
      "3 dudas pendientes de revisar.",
    );

    await push({ type: "notice", pending_count: 1, server_time_ms: NOW });
    expect(screen.getByRole("status", { name: "Dudas pendientes" })).toHaveTextContent(
      "1 duda pendiente de revisar.",
    );

    await push({ type: "notice", pending_count: 0, server_time_ms: NOW });
    expect(screen.getByRole("status", { name: "Dudas pendientes" })).toHaveTextContent(
      "No hay dudas pendientes de revisar.",
    );
  });

  it("warns while the server's recognizer is degraded and clears the warning when it recovers", async () => {
    renderScreen();
    await open("server");
    const warning = () => screen.queryByRole("alert", { name: "Estado de la transcripción" });
    expect(warning()).not.toBeInTheDocument();

    const lost = "Se ha perdido la conexión con Google Cloud; se reintenta en 5 s.";
    await push({ type: "stt.status", state: "reconnecting", detail: lost, server_time_ms: NOW });
    expect(warning()).toHaveTextContent(lost);

    await push({ type: "stt.status", state: "ok", server_time_ms: NOW });
    expect(warning()).not.toBeInTheDocument();

    // A status without a detail of its own still says what it means.
    await push({ type: "stt.status", state: "unavailable", server_time_ms: NOW });
    expect(warning()).toHaveTextContent("La transcripción del servidor no está disponible");
    // The session goes on: nothing blocks the page.
    expect(screen.getByRole("button", { name: "Importante" })).toBeEnabled();
  });

  it("takes a burst and answers the ack when the backend asks for capture_now", async () => {
    renderScreen();
    await open();

    await push({
      type: "command",
      command_id: "cmd-1",
      command: "capture_now",
      server_time_ms: NOW,
    });

    expect(fakes.photos.taken).toHaveLength(3);
    const metadata = await metadataOf(capturePost().init);
    expect(() => decodeCaptureUploadRequest(metadata, "")).not.toThrow();
    expect(metadata.trigger).toBe("command");
    expect(metadata.command_id).toBe("cmd-1");
    expect(lastFrame()).toEqual({ type: "ack", command_id: "cmd-1", client_time_ms: NOW });
    expect(await screen.findByText("Guardada")).toBeInTheDocument();
  });

  it("answers the ack of a capture_now even when the camera refuses the burst", async () => {
    fakes.devices.failure = "NotAllowedError";
    renderScreen();
    await open("client", false);

    await push({
      type: "command",
      command_id: "cmd-2",
      command: "capture_now",
      server_time_ms: NOW,
    });

    // A backend left without an ack waits for a client that has already given up (ADR-0006).
    expect(lastFrame()).toEqual({ type: "ack", command_id: "cmd-2", client_time_ms: NOW });
    expect(await screen.findByText("Error")).toBeInTheDocument();
  });
});

describe("CapturePage", () => {
  it("runs the picker, then the session, and comes back when the session ends", async () => {
    backend({
      "/api/subjects": () =>
        jsonResponse({ subjects: [{ subject_id: "biologia", name: "Biología" }] }),
      "/api/subjects/biologia/topics": () =>
        jsonResponse({
          subject_id: "biologia",
          topics: [{ topic_id: "fotosintesis", subject_id: "biologia", name: "Fotosíntesis" }],
        }),
      "/api/sessions": () => jsonResponse(SESSION, 201),
    });
    render(<CapturePage now={() => NOW} />);

    fireEvent.click(await screen.findByRole("button", { name: "Biología" }));
    fireEvent.click(await screen.findByRole("button", { name: "Fotosíntesis" }));
    fireEvent.click(await screen.findByRole("button", { name: "Empezar una sesión nueva" }));

    await waitFor(() => expect(fakes.sockets).toHaveLength(1));
    expect(socket().url).toContain(SESSION.ws_path);
    await open();
    expect(screen.getByRole("button", { name: "Capturar" })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Terminar" }));
    });

    expect(await screen.findByRole("heading", { name: "Asignaturas" })).toBeInTheDocument();
    expect(fakes.videoTrack.readyState).toBe("ended");
  });

  it("leads from the chosen topic to the voice tutor and back, without opening a session", async () => {
    backend({
      "/api/subjects": () =>
        jsonResponse({ subjects: [{ subject_id: "biologia", name: "Biología" }] }),
      "/api/subjects/biologia/topics": () =>
        jsonResponse({
          subject_id: "biologia",
          topics: [{ topic_id: "fotosintesis", subject_id: "biologia", name: "Fotosíntesis" }],
        }),
      "/api/subjects/biologia/topics/fotosintesis/tutor": () =>
        jsonResponse({ subject: "biologia", topic: "fotosintesis", turns: [] }),
    });
    render(<CapturePage now={() => NOW} />);

    fireEvent.click(await screen.findByRole("button", { name: "Biología" }));
    fireEvent.click(await screen.findByRole("button", { name: "Fotosíntesis" }));
    fireEvent.click(await screen.findByRole("button", { name: "Preguntar al tutor" }));

    expect(await screen.findByRole("heading", { name: "Preguntar al tutor" })).toBeInTheDocument();
    expect(screen.getByText(/Biología · Fotosíntesis/)).toBeInTheDocument();
    expect(
      await screen.findByText("Todavía no le has preguntado nada sobre este tema."),
    ).toBeInTheDocument();
    expect(fakes.sockets).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "← Volver" }));
    expect(await screen.findByRole("heading", { name: "Asignaturas" })).toBeInTheDocument();
  });
});
