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
import { HEALTH_POLL_MS } from "./sessionHealth";
import {
  type CaptureFakes,
  FakeBiasingSpeechRecognition,
  FakeSpeechRecognitionPhrase,
  type FakeWebSocket,
  installCaptureFakes,
  setVisibility,
  swapGlobal,
  swapProperty,
} from "./testing";
import { LOAD_TIMEOUT, PAGE_TEST_TIMEOUT } from "../test/timeouts";

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
const RESUME_PATH = `/api/sessions/${SESSION.session_id}/resume`;
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
    [RESUME_PATH]: () => jsonResponse({ ...SESSION, received_capture_ids: [] }),
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

describe("the session health line (#262)", () => {
  const HEALTH_PATH = `/api/sessions/${SESSION.session_id}/health`;
  const quiet = { count: 0, message: null };

  it("stays hidden while healthy and shows a discreet line when the observer fails", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"], shouldAdvanceTime: true });
    try {
      const answers = [
        { ok: true, observer: quiet },
        { ok: false, observer: { count: 3, message: "Claude no responde (sin conexión o saturado)" } },
      ];
      backend({
        [HEALTH_PATH]: () =>
          jsonResponse({
            session_id: SESSION.session_id,
            observer_paused: false,
            observer_paused_message: null,
            transcription: quiet,
            push: quiet,
            ...(answers.shift() ?? answers[0]),
          }),
      });
      renderScreen();
      await open();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS);
      });
      expect(sent.some((call) => call.path === HEALTH_PATH)).toBe(true);
      expect(screen.queryByRole("status", { name: "Estado del servidor" })).toBeNull();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS);
      });
      expect(screen.getByRole("status", { name: "Estado del servidor" })).toHaveTextContent(
        "El observador ha fallado 3 veces: Claude no responde (sin conexión o saturado).",
      );
      expect(screen.queryByRole("dialog")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  }, PAGE_TEST_TIMEOUT);
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

  it("says in Spanish when the backend connection stays lost for a long outage (#411)", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"], shouldAdvanceTime: true });
    try {
      backend({ [RESUME_PATH]: () => Promise.reject(new TypeError("Failed to fetch")) });
      renderScreen({ longOutageMs: 120_000, reconnectDelaysMs: [1000, 30_000] });
      await open();

      await act(async () => {
        socket().serverClose(1006);
      });
      expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
        "Reconectando…",
      );
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Capturar" })).toBeEnabled();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(119_000);
      });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Se ha perdido la conexión con el servidor.",
      );
      expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
        "Se ha perdido la conexión con el servidor",
      );
      expect(screen.getByRole("button", { name: "Capturar" })).toBeDisabled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("says in Spanish when the backend connection fails before it opens (#298)", async () => {
    renderScreen();

    await act(async () => {
      socket().serverError();
      socket().serverClose(1006);
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El servidor ha cerrado la conexión antes de aceptar esta página.",
    );
    expect(socket().sent).toEqual([]);
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

    expect(await screen.findByRole("alert", { name: "Cámara desconectada" })).toHaveTextContent(
      "La cámara se ha desconectado",
    );
    expect(screen.getByRole("button", { name: "Capturar" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reactivar cámara" })).toBeEnabled();
    // Only the camera is gone: the session and its socket carry on.
    expect(socket().closeCalls).toEqual([]);
    expect(screen.getByRole("button", { name: "Importante" })).toBeEnabled();
  });

  it("asks for the camera again on Reactivar cámara and resumes the preview", async () => {
    const { container } = renderScreen();
    await open();
    await act(async () => {
      fakes.videoTrack.endFromDevice();
    });
    const replugged = fakes.devices.plugInCamera();

    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Reactivar cámara" }));
    });

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
        "La cámara está en marcha.",
      ),
    );
    expect(fakes.devices.getUserMediaCalls).toHaveLength(2);
    expect(container.querySelector("video")?.srcObject).toBe(fakes.devices.streams[1]);
    expect(fakes.devices.streams[1].getVideoTracks()).toEqual([replugged]);
    expect(screen.queryByRole("alert", { name: "Cámara desconectada" })).toBeNull();
    expect(screen.getByRole("button", { name: "Capturar" })).toBeEnabled();
    expect(socket().closeCalls).toEqual([]);

    await capture();
    await waitFor(() => expect(capturePost()).toBeDefined());
  }, PAGE_TEST_TIMEOUT);

  it("keeps the notice up with the reason when the camera cannot come back yet", async () => {
    renderScreen();
    await open();
    await act(async () => {
      fakes.videoTrack.endFromDevice();
    });
    fakes.devices.failure = "NotReadableError";

    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Reactivar cámara" }));
    });

    const notice = await screen.findByRole("alert", { name: "Cámara desconectada" });
    await waitFor(() =>
      expect(notice).toHaveTextContent("Otra aplicación está usando la cámara."),
    );
    expect(screen.getByRole("button", { name: "Reactivar cámara" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Capturar" })).toBeDisabled();
  });
});

describe("a long session on a laptop", () => {
  it("holds the screen awake while the session runs and lets it go on Terminar", async () => {
    renderScreen();
    await open();

    expect(fakes.wakeLock.held()).toHaveLength(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Terminar" }));
    });
    await waitFor(() => expect(fakes.wakeLock.held()).toHaveLength(0));
  });

  it("asks for the wake lock again when the tab comes back and lets it go on unmount", async () => {
    restores.push(swapProperty(document, "visibilityState", "visible"));
    const { unmount } = renderScreen();
    await open();

    await act(async () => {
      restores.push(setVisibility("hidden"));
      fakes.wakeLock.sentinels[0].releaseFromBrowser();
    });
    await act(async () => {
      restores.push(setVisibility("visible"));
    });

    await waitFor(() => expect(fakes.wakeLock.requestCount).toBe(2));
    expect(fakes.wakeLock.held()).toHaveLength(1);
    unmount();
    expect(fakes.wakeLock.held()).toHaveLength(0);
  });

  it("runs the session as usual in a browser without the wake lock API", async () => {
    fakes.restore();
    fakes = installCaptureFakes({ wakeLock: false });
    renderScreen();
    await open();

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Capturar" })).toBeEnabled();
  });

});

describe("a hidden tab pauses the capture (#425)", () => {
  function buttons(): string[] {
    return frames()
      .filter((frame) => frame.type === "button")
      .map((frame) => String(frame.button));
  }

  it("stops the camera and the recognizer, says pause and shows the paused state", async () => {
    renderScreen();
    await open();
    expect(screen.queryByRole("status", { name: "Captura en pausa" })).toBeNull();
    const recognition = fakes.recognitions[0];

    await act(async () => {
      restores.push(setVisibility("hidden"));
    });

    expect(buttons()).toEqual(["pause"]);
    expect(fakes.videoTrack.readyState).toBe("ended");
    expect(recognition.abortCount + recognition.stopCount).toBeGreaterThan(0);
    expect(screen.getByRole("status", { name: "Captura en pausa" })).toHaveTextContent(
      "Captura en pausa: la pestaña está oculta",
    );
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
      "La cámara no está en marcha.",
    );
    // The socket stays open: the reconnect logic of #411 is not disturbed.
    expect(socket().closeCalls).toEqual([]);
  });

  it("says resume and starts the camera and the recognizer again when the tab is back", async () => {
    renderScreen();
    await open();
    await act(async () => {
      restores.push(setVisibility("hidden"));
    });

    await act(async () => {
      restores.push(setVisibility("visible"));
    });

    expect(buttons()).toEqual(["pause", "resume"]);
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
        "La cámara está en marcha.",
      ),
    );
    expect(fakes.devices.getUserMediaCalls.filter((call) => call.video)).toHaveLength(2);
    await waitFor(() => expect(fakes.recognitions).toHaveLength(2));
    expect(fakes.recognitions[1].startCount).toBe(1);
    expect(screen.queryByRole("status", { name: "Captura en pausa" })).toBeNull();
    expect(socket().closeCalls).toEqual([]);
  });

  it("stops the audio stream in server mode and starts it again", async () => {
    renderScreen();
    await open("server");
    expect(fakes.workletNodes).toHaveLength(1);

    await act(async () => {
      restores.push(setVisibility("hidden"));
    });
    expect(buttons()).toEqual(["pause"]);
    expect(fakes.audioTrack.readyState).toBe("ended");

    await act(async () => {
      restores.push(setVisibility("visible"));
    });
    expect(buttons()).toEqual(["pause", "resume"]);
    await waitFor(() => expect(fakes.workletNodes).toHaveLength(2));
    expect(fakes.recognitions).toHaveLength(0);
  });

  it("says the session ended for inactivity when the backend ended it while nobody sent", async () => {
    renderScreen();
    await open();

    await act(async () => {
      socket().serverClose(4404, "session s ended: no capture client was sending (idle)");
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La sesión terminó por inactividad",
    );
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "La sesión ha terminado",
    );
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

  it("sends no prepare_notes on plain Terminar", async () => {
    renderScreen();
    await open();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Terminar" }));
    });
    const post = sent.find((call) => call.path === END_PATH);
    expect(post).toBeDefined();
    expect(JSON.parse(post?.init.body as string)).not.toHaveProperty("prepare_notes");
  });

  it("is a section with an h2 and no doubts line of its own inside the workspace (#413)", async () => {
    renderScreen({ embedded: true });
    await open();
    await push({ type: "notice", pending_count: 3, server_time_ms: NOW });
    expect(screen.queryByRole("main")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Captura en curso" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Capturar una sesión de estudio" })).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Dudas pendientes" })).not.toBeInTheDocument();
    expect(screen.queryByText(/dudas pendientes/)).not.toBeInTheDocument();
  });

  it("keeps its main, h1 and doubts line on /capture", async () => {
    renderScreen();
    await open();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Capturar una sesión de estudio" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Dudas pendientes" })).toBeInTheDocument();
  });

  describe("no «Terminar y preparar apuntes» on the web (#413)", () => {
    for (const embedded of [false, true]) {
      it(`offers only Terminar ${embedded ? "inside the workspace" : "on /capture"}`, async () => {
        renderScreen({ embedded });
        await open();
        for (const name of ["Capturar", "Importante", "Libro", "Apuntes", "Terminar"]) {
          expect(screen.getByRole("button", { name })).toBeInTheDocument();
        }
        expect(screen.queryByRole("button", { name: /preparar apuntes/ })).not.toBeInTheDocument();
      });
    }
  });

  describe("the backend closes the socket while the session ends (#319)", () => {
    /** An end route the test answers itself, after the backend's close. */
    function heldEnd(): { answer: (response: Response) => void } {
      let answer!: (response: Response) => void;
      const held = new Promise<Response>((resolve) => {
        answer = resolve;
      });
      backend({ [END_PATH]: () => held });
      return { answer };
    }

    for (const code of [4404, 1000, 1006]) {
      it(`is no lost connection when the close (${code}) comes before the end's answer`, async () => {
        const end = heldEnd();
        const ended = vi.fn();
        renderScreen({ onEnded: ended });
        await open();

        await act(async () => {
          fireEvent.click(screen.getByRole("button", { name: "Terminar" }));
        });
        await act(async () => {
          socket().serverClose(code, code === 4404 ? "session s is no longer active" : "");
        });
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
        await act(async () => {
          end.answer(jsonResponse(ENDED));
        });

        await waitFor(() => expect(ended).toHaveBeenCalledWith(ENDED));
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
        expect(screen.queryByText(/Se ha perdido la conexión/)).not.toBeInTheDocument();
      });
    }

    it("shows the lost connection too when the end then fails", async () => {
      const end = heldEnd();
      renderScreen();
      await open();

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Terminar" }));
      });
      await act(async () => {
        socket().serverClose(1006);
      });
      await act(async () => {
        end.answer(jsonResponse({ detail: "El servidor no ha podido terminar la sesión." }, 500));
      });

      const alerts = await screen.findAllByRole("alert");
      const text = alerts.map((alert) => alert.textContent).join(" ");
      expect(text).toContain("No se ha podido terminar la sesión");
      expect(text).toContain("Se ha perdido la conexión con el servidor.");
      expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
        "Se ha perdido la conexión con el servidor",
      );
    });
  });

  it("says the session has ended when the backend closes it with 4404 mid-capture", async () => {
    renderScreen();
    await open();

    await act(async () => {
      socket().serverClose(4404, "session s is no longer active");
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La sesión ha terminado en el servidor.",
    );
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "La sesión ha terminado",
    );
    expect(screen.queryByText(/Se ha perdido la conexión/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Capturar" })).toBeDisabled();
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
    expect(transcript().getByText("la célul")).toHaveClass("capture-segment-partial");

    await push({ type: "transcript.final", ...segment, text: "la célula" });
    // The final settles the utterance: one item, the interim text gone.
    expect(transcript().getAllByRole("listitem")).toHaveLength(1);
    expect(transcript().getByText("la célula")).toHaveClass("capture-segment-final");
    expect(transcript().getByText("la célula")).not.toHaveClass("capture-segment-partial");
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

    // Since #413 the end points to the topic's workspace, and the picker is one click away.
    expect(await screen.findByRole("heading", { name: "Sesión terminada" })).toBeInTheDocument();
    expect(fakes.videoTrack.readyState).toBe("ended");
    expect(screen.getByText(/prepárame el tema/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Abrir en Construir" })).toHaveAttribute(
      "href",
      "/subjects/biologia/topics/fotosintesis/workspace",
    );
    expect(screen.queryByRole("button", { name: /preparar apuntes/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Volver a la lista de sesiones" }));
    expect(await screen.findByRole("heading", { name: "Asignaturas" })).toBeInTheDocument();
  }, PAGE_TEST_TIMEOUT);

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
  }, PAGE_TEST_TIMEOUT);
});

describe("a dropped connection (#411)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function fakeTimers(): void {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"], shouldAdvanceTime: true });
  }

  /** Lets the socket's backoff run out, its resume answer, and the new connection say hello. */
  async function reconnect(
    delayMs = 1000,
    sttMode: "client" | "server" = "client",
  ): Promise<FakeWebSocket> {
    const count = fakes.sockets.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(delayMs);
    });
    await waitFor(() => expect(fakes.sockets).toHaveLength(count + 1), { timeout: LOAD_TIMEOUT });
    await act(async () => {
      socket().serverOpen();
      socket().serverMessage(helloAck(sttMode));
    });
    return socket();
  }

  it("reconnects on its own, keeps camera and recognizer running and flushes what was said", async () => {
    fakeTimers();
    renderScreen({ reconnectDelaysMs: [1000] });
    await open();
    const recognition = fakes.recognitions[0];

    await act(async () => {
      socket().serverClose(1006);
    });
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Reconectando…",
    );
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
      "La cámara está en marcha.",
    );
    expect(recognition.abortCount).toBe(0);

    await act(async () => {
      recognition.emitResult([{ transcript: "la clorofila absorbe la luz", final: true }]);
    });

    const next = await reconnect();
    expect(sent.some((call) => call.path === RESUME_PATH)).toBe(true);
    const types = next.sentText.map((text) => (JSON.parse(text) as SentFrame).type);
    expect(types).toEqual(["hello", "transcript.client.final"]);
    expect(JSON.parse(next.sentText[1])).toMatchObject({ text: "la clorofila absorbe la luz" });
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Conexión recuperada",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Conectado con el servidor",
    );
  });

  it("says pause again on a new connection while the tab is hidden (#425)", async () => {
    fakeTimers();
    renderScreen({ reconnectDelaysMs: [1000] });
    await open();
    await act(async () => {
      restores.push(setVisibility("hidden"));
    });

    await act(async () => {
      socket().serverClose(1006);
    });
    const next = await reconnect();

    const sentFrames = next.sentText.map((text) => JSON.parse(text) as SentFrame);
    expect(sentFrames[0].type).toBe("hello");
    expect(sentFrames.filter((frame) => frame.type === "button").at(-1)).toMatchObject({
      button: "pause",
    });
  });

  /** A tenth of a second of a 16 kHz microphone, as the worklet processor hands it over. */
  async function speak(startTimeSec: number): Promise<void> {
    const chunk: PcmWorkletChunk = {
      samples: Float32Array.from({ length: 1600 }, () => 0.25),
      startTimeSec,
    };
    await act(async () => {
      fakes.workletNodes[0].emitProcessorMessage(chunk);
    });
  }

  /** The `seq` of every audio frame a connection carried. */
  function audioSeqs(fake: FakeWebSocket): number[] {
    return fake.sentBinary.map((bytes) =>
      new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(6, false),
    );
  }

  it("reconnects in server STT mode too and streams from frame 0 to a restarted backend (#419)", async () => {
    fakeTimers();
    renderScreen({ reconnectDelaysMs: [1000] });
    await open("server");
    await speak(0);
    await speak(0.1);
    const first = socket();
    expect(audioSeqs(first)).toEqual([0, 1]);

    await act(async () => {
      first.serverClose(1012);
    });
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Reconectando…",
    );
    // The microphone goes on; what it hears during the outage is not sent anywhere.
    expect(fakes.audioTrack.readyState).toBe("live");
    await speak(0.2);

    const next = await reconnect(1000, "server");
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Conexión recuperada",
    );
    await speak(0.3);
    await speak(0.4);

    expect(audioSeqs(first)).toEqual([0, 1]);
    expect(audioSeqs(next)).toEqual([0, 1]);
    expect(fakes.workletNodes).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says pause again on a new connection in server STT mode while the tab is hidden (#419)", async () => {
    fakeTimers();
    renderScreen({ reconnectDelaysMs: [1000] });
    await open("server");
    await act(async () => {
      restores.push(setVisibility("hidden"));
    });
    expect(fakes.audioTrack.readyState).toBe("ended");

    await act(async () => {
      socket().serverClose(1006);
    });
    const next = await reconnect(1000, "server");

    const sentFrames = next.sentText.map((text) => JSON.parse(text) as SentFrame);
    expect(sentFrames[0].type).toBe("hello");
    expect(sentFrames.filter((frame) => frame.type === "button").at(-1)).toMatchObject({
      button: "pause",
    });
    expect(next.sentBinary).toEqual([]);
    expect(fakes.workletNodes).toHaveLength(1);
  });

  it("switches from the recognizer to the audio stream when the backend comes back in server mode (#447)", async () => {
    fakeTimers();
    renderScreen({ reconnectDelaysMs: [1000] });
    await open("client");
    const recognition = fakes.recognitions[0];
    await act(async () => {
      socket().serverClose(1012);
    });

    const next = await reconnect(1000, "server");

    expect(recognition.abortCount).toBeGreaterThan(0);
    await waitFor(() => expect(fakes.workletNodes).toHaveLength(1), { timeout: LOAD_TIMEOUT });
    expect(fakes.recognitions).toHaveLength(1);
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Transcribe el servidor",
    );
    // The camera went on through the switch.
    expect(fakes.devices.getUserMediaCalls.filter((call) => call.video)).toHaveLength(1);
    expect(screen.getByRole("status", { name: "Estado de la cámara" })).toHaveTextContent(
      "La cámara está en marcha.",
    );
    await speak(0);
    expect(audioSeqs(next)).toEqual([0]);
    expect(next.closeCalls).toEqual([]);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("switches from the audio stream to the recognizer when the backend comes back in client mode (#447)", async () => {
    fakeTimers();
    renderScreen({ reconnectDelaysMs: [1000] });
    await open("server");
    expect(fakes.recognitions).toHaveLength(0);
    await act(async () => {
      socket().serverClose(1012);
    });

    const next = await reconnect(1000, "client");

    expect(fakes.audioTrack.readyState).toBe("ended");
    await waitFor(() => expect(fakes.recognitions).toHaveLength(1), { timeout: LOAD_TIMEOUT });
    const recognition = fakes.recognitions[0];
    expect(recognition.startCount).toBe(1);
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Transcribe este navegador",
    );
    await act(async () => {
      recognition.emitResult([{ transcript: "el estroma", final: true }]);
    });
    const types = next.sentText.map((text) => (JSON.parse(text) as SentFrame).type);
    expect(types).toEqual(["hello", "transcript.client.final"]);
    expect(next.sentBinary).toEqual([]);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("starts the new mode's transcriber only when a hidden tab comes back (#447)", async () => {
    fakeTimers();
    renderScreen({ reconnectDelaysMs: [1000] });
    await open("client");
    await act(async () => {
      restores.push(setVisibility("hidden"));
    });
    await act(async () => {
      socket().serverClose(1012);
    });

    await reconnect(1000, "server");
    expect(fakes.workletNodes).toHaveLength(0);
    expect(screen.getByRole("status", { name: "Estado de la conexión" })).toHaveTextContent(
      "Transcribe el servidor",
    );

    await act(async () => {
      restores.push(setVisibility("visible"));
    });
    await waitFor(() => expect(fakes.workletNodes).toHaveLength(1), { timeout: LOAD_TIMEOUT });
    expect(fakes.recognitions).toHaveLength(1);
  });

  it("clears the long-outage message once the connection comes back", async () => {
    fakeTimers();
    renderScreen({ longOutageMs: 3000, reconnectDelaysMs: [5000] });
    await open();

    await act(async () => {
      socket().serverClose(1006);
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(screen.getByRole("alert")).toHaveTextContent("Se ha perdido la conexión con el servidor.");

    await reconnect(2000);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Capturar" })).toBeEnabled();
  });

  it("says the session ended when the resume finds it over", async () => {
    fakeTimers();
    backend({
      [RESUME_PATH]: () => jsonResponse({ detail: "La sesión ya ha terminado." }, 409),
    });
    renderScreen({ reconnectDelaysMs: [1000] });
    await open();

    await act(async () => {
      socket().serverClose(1006);
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La sesión ha terminado en el servidor. Vuelve a la lista de sesiones",
    );
    expect(fakes.sockets).toHaveLength(1);
  });

  it("points to Construir and Estudiar when the session ended elsewhere inside the workspace", async () => {
    renderScreen({ embedded: true });
    await open();

    await act(async () => {
      socket().serverClose(4404, "session s is no longer active");
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Construir");
    expect(alert).toHaveTextContent("Estudiar");
    expect(alert).not.toHaveTextContent("lista de sesiones");
  });

  it("retries after the reconnect a burst whose upload failed on the network", async () => {
    fakeTimers();
    let down = true;
    backend({
      [CAPTURES_PATH]: (init) =>
        down ? Promise.reject(new TypeError("Failed to fetch")) : storedResponse(init),
    });
    renderScreen({ reconnectDelaysMs: [1000] });
    await open();

    await act(async () => {
      socket().serverClose(1006);
    });
    await capture();
    expect(await screen.findByText("Pendiente de subir")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    down = false;
    await reconnect();

    expect(await screen.findByText("Guardada")).toBeInTheDocument();
    const posts = sent.filter((call) => call.path === CAPTURES_PATH);
    expect(posts).toHaveLength(2);
    const ids = await Promise.all(posts.map(async (post) => (await metadataOf(post.init)).capture_id));
    expect(ids[1]).toBe(ids[0]);
  });

  it("does not upload again a burst the resume says is stored", async () => {
    fakeTimers();
    let storedId: string | null = null;
    backend({
      [CAPTURES_PATH]: async (init) => {
        storedId = (await metadataOf(init)).capture_id;
        return jsonResponse({ detail: "Service Unavailable" }, 503);
      },
      [RESUME_PATH]: () =>
        jsonResponse({ ...SESSION, received_capture_ids: storedId === null ? [] : [storedId] }),
    });
    renderScreen({ reconnectDelaysMs: [1000] });
    await open();

    await act(async () => {
      socket().serverClose(1006);
    });
    await capture();
    expect(await screen.findByText("Pendiente de subir")).toBeInTheDocument();

    await reconnect();

    expect(await screen.findByText("Guardada")).toBeInTheDocument();
    expect(sent.filter((call) => call.path === CAPTURES_PATH)).toHaveLength(1);
  });

  it("keeps a burst the backend refused with a 4xx as an error, and does not retry it", async () => {
    fakeTimers();
    backend({ [CAPTURES_PATH]: () => jsonResponse({ detail: "La ráfaga no es válida." }, 422) });
    renderScreen({ reconnectDelaysMs: [1000] });
    await open();

    await capture();
    expect(await screen.findByText("Error")).toBeInTheDocument();
    await act(async () => {
      socket().serverClose(1006);
    });
    await reconnect();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(sent.filter((call) => call.path === CAPTURES_PATH)).toHaveLength(1);
    expect(screen.getByText("Error")).toBeInTheDocument();
  });

  it("warns before the page is left only while work is pending", async () => {
    fakeTimers();
    let answer: ((response: Response) => void) | undefined;
    backend({
      [CAPTURES_PATH]: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    renderScreen({ reconnectDelaysMs: [1000] });
    await open();

    const leave = (): boolean => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(leave()).toBe(false);

    await capture();
    expect(await screen.findByText("Subiendo…")).toBeInTheDocument();
    expect(leave()).toBe(true);

    await act(async () => {
      answer?.(await storedResponse(capturePost().init));
    });
    expect(await screen.findByText("Guardada")).toBeInTheDocument();
    expect(leave()).toBe(false);

    // A final said during an outage waits in the socket's queue until the resume.
    await act(async () => {
      socket().serverClose(1006);
      fakes.recognitions[0].emitResult([{ transcript: "el estroma", final: true }]);
    });
    expect(leave()).toBe(true);
    await reconnect();
    expect(leave()).toBe(false);
  });
});
