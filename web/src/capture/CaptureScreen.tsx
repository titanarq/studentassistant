/**
 * The capture screen of a running session (#40): the camera preview, the student's buttons, the
 * burst thumbnails and the live transcript. It is the half of `/capture` the picker gives way to,
 * and the place where the rest of `capture/` meets: `SessionSocket` for the wire, the
 * `ClientTranscriber` that `hello.ack.stt_mode` picks and `Camera` for the page photos.
 *
 * Two rules shape it. Everything the student reads is Spanish, including every way a device, a
 * browser or the connection can refuse: the codes `camera.ts` and `transcriber.ts` report are this
 * page's own vocabulary and each one has a sentence here, so a refusal is never a technical string
 * and never silent. And the page owns no protocol knowledge of its own -- it sends through the
 * socket and the REST client and reads only what the bindings decoded.
 *
 * The screen runs one session and stops it on the way out, whatever the reason: a component that
 * left a recognizer, a camera or a socket behind would keep a laptop's light on and a session open
 * after the student moved on.
 *
 * A dropped connection is not the end of the capture (#411). The socket reconnects on its own
 * (resuming the session first, which a restarted backend needs), in either STT mode (#419),
 * while the camera and the recognizer (or the audio stream) keep running and what the student says waits for the resume; the page says
 * «Reconectando…» in its status line and only after a long outage (`LONG_OUTAGE_MS`) shows the
 * blocking message. A burst whose upload failed on the network or a 5xx waits as «Pendiente de
 * subir» and goes up again after the reconnect; the page asks before being left while any upload
 * or queued frame is still pending.
 *
 * A reconnect that lands on a backend restarted in the other STT mode switches the transcriber to
 * the new mode's one (#447): the old one would send frames that backend refuses.
 *
 * The capture runs only while the tab is visible (#425). A hidden tab stops sending: the camera and
 * the recognizer (or the audio stream) stop, the socket stays open and says `button: pause`, and the
 * page shows «Captura en pausa». Visible again, it says `resume` and starts both again. Since #450
 * the workspace pauses it the same way while its **Recursos** tab is shown (`suspended`). A session no
 * client sends to for a while is ended by the backend itself, which the page reads as «La sesión
 * terminó por inactividad».
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ClientCapabilities,
  HelloAck,
  PauseReason,
  Session,
  SessionEndResponse,
  SourceKind,
  TranscriptFinal,
  TranscriptPartial,
} from "../protocol";
import { type CapturesResult, endSession, resumeSession, uploadCaptures } from "./api";
import { AudioStreamTranscriber, audioStreamSupported } from "./audioStreamTranscriber";
import {
  type BurstTrigger,
  Camera,
  type CapturedBurst,
  type CameraProblemCode,
} from "./camera";
import { describeFailure } from "./failures";
import {
  CAPTURE_CAPABILITIES,
  type ResumeOutcome,
  SESSION_NOT_ACTIVE_CLOSE,
  SessionSocket,
  type SessionSocketEvent,
  WEB_SPEECH_PROVIDER,
} from "./sessionSocket";
import { type ClientTranscriber, type TranscriberProblemCode } from "./transcriber";
import { useSessionHealth } from "./sessionHealth";
import { ScreenWakeLock } from "./wakeLock";
import { WebSpeechTranscriber } from "./webSpeechTranscriber";
import "./capture.css";

/** How long a burst's flash covers the preview: long enough to see, short enough to not miss. */
export const FLASH_MS = 160;

/** How long an outage stays a status line before the page says the connection is lost (#411). */
export const LONG_OUTAGE_MS = 120_000;

/** How long «Conexión recuperada» stays in the status line after a reconnect. */
export const RECOVERED_MS = 5_000;

/** How long a burst whose upload failed while the connection was up waits before it goes up again. */
export const UPLOAD_RETRY_MS = 10_000;

/**
 * The partial's muted grey and the final's full ink colour, as classes of `capture.css` so both
 * follow the light and dark themes of the design system (#296).
 */
const PARTIAL_CLASS = "capture-segment capture-segment-partial";
const FINAL_CLASS = "capture-segment capture-segment-final";

/**
 * How a burst's upload is going, in the words the strip shows. `pendiente` (#411) is an upload
 * that failed on the network or the server's side and will be sent again; `error` is final.
 */
type BurstState = "subiendo" | "pendiente" | "guardada" | "duplicada" | "error";

/**
 * The session's connection, in the four states the page shows: it is opening, the backend accepted
 * it, it ended by itself, or this page could never open one. `unavailable` stays apart from `lost`
 * because a student who loaded the page from an address the browser does not trust has nothing to
 * reconnect to.
 */
type ConnectionState = "connecting" | "open" | "reconnecting" | "lost" | "ended" | "unavailable";

const CONNECTION_TEXT: Record<ConnectionState, string> = {
  connecting: "Conectando con el servidor…",
  open: "Conectado con el servidor",
  reconnecting: "Reconectando…",
  lost: "Se ha perdido la conexión con el servidor",
  ended: "La sesión ha terminado",
  unavailable: "Esta página no puede abrir la sesión",
};

/** What a degraded `stt.status` means when it carries no `detail` of its own (protocol 1.5). */
const STT_DEGRADED: Record<"reconnecting" | "unavailable", string> = {
  reconnecting:
    "El servidor ha perdido la transcripción y está reintentando; lo que digas mientras tanto no se transcribe.",
  unavailable: "La transcripción del servidor no está disponible; lo que digas no se transcribe.",
};

const BURST_LABELS: Record<BurstState, string> = {
  subiendo: "Subiendo…",
  pendiente: "Pendiente de subir",
  guardada: "Guardada",
  // A `capture_id` the backend already had: the photographs are stored, so this is not a failure.
  duplicada: "Duplicada",
  error: "Error",
};

const CAMERA_MESSAGES: Record<CameraProblemCode, string> = {
  unsupported:
    "Este navegador no puede abrir la cámara. Usa Chrome o Edge actualizados en este ordenador.",
  "permission-denied":
    "El navegador ha bloqueado la cámara. Dale permiso a esta página desde el icono de la barra de direcciones y vuelve a cargarla.",
  "missing-device":
    "No se ha encontrado ninguna cámara. Conecta una y vuelve a cargar la página.",
  "in-use": "Otra aplicación está usando la cámara. Ciérrala y vuelve a cargar la página.",
  lost: "La cámara se ha desconectado o otra aplicación se la ha quedado. Vuelve a conectarla o cierra esa aplicación y pulsa «Reactivar cámara».",
  unavailable: "La cámara no ha funcionado. Cierra la página, vuelve a abrirla e inténtalo otra vez.",
};

const TRANSCRIBER_MESSAGES: Record<TranscriberProblemCode, string> = {
  unsupported:
    "Este navegador no reconoce el habla por sí mismo. Usa Chrome o Edge, o configura en el servidor un proveedor de transcripción.",
  "permission-denied":
    "El navegador ha bloqueado el micrófono. Dale permiso a esta página desde el icono de la barra de direcciones y vuelve a cargarla.",
  network:
    "El servicio de reconocimiento de voz no responde. La página lo vuelve a intentar por su cuenta.",
  unavailable: "El micrófono no ha funcionado para la transcripción.",
};

/**
 * What the student reads when the page cannot do its job at all. The detail of the failure is the
 * backend's or the browser's own English wording: it goes in the element's `title`, where whoever
 * debugs it finds it, and never in the sentence.
 */
interface Blocking {
  readonly message: string;
  readonly detail?: string;
}

const INSECURE: Blocking = {
  message:
    "Esta página necesita un origen seguro para usar la cámara y el micrófono. Ábrela en http://localhost, en el mismo ordenador donde corre el servidor.",
};

const DISCONNECTED: Blocking = {
  message:
    "Se ha perdido la conexión con el servidor. Comprueba que sigue en marcha y vuelve a abrir la sesión.",
};

/** The backend closed the socket because the session ended without this page's Terminar (#319). */
const ENDED_ELSEWHERE: Blocking = {
  message:
    "La sesión ha terminado en el servidor. Vuelve a la lista de sesiones para empezar o reanudar otra.",
};

/**
 * The same inside the study workspace (#411), where there is no list of sessions to go back to:
 * the topic goes on in Construir, where the page is, or in Estudiar.
 */
const ENDED_ELSEWHERE_EMBEDDED: Blocking = {
  message:
    "La sesión ha terminado en el servidor. Sigue con este tema en Construir, con el chat de esta pantalla, o pasa a Estudiar.",
};

const HANDSHAKE_REFUSED: Blocking = {
  message:
    "El servidor ha rechazado la conexión: esta página y él no hablan la misma versión del protocolo.",
};

const HANDSHAKE_LOST: Blocking = {
  message: "El servidor ha cerrado la conexión antes de aceptar esta página.",
};

const PROTOCOL_REFUSED: Blocking = {
  message: "El servidor ha enviado un mensaje que no sigue el protocolo esperado.",
};

/**
 * What the page says while its tab is hidden (#425): the capture is paused, not lost. It replaced
 * the #256 notice that transcription may have paused in a hidden tab: now it always does, on
 * purpose, and starts again when the tab is back.
 */
const PAUSED_NOTICE = "Captura en pausa: la pestaña está oculta";

/** The same while the workspace shows **Recursos** instead (#450). */
const SUSPENDED_NOTICE = "Captura en pausa: vuelve a la pestaña Captura para seguir";

/**
 * What the backend's close reason ends with when it ended the session itself because no capture
 * client was sending (#425, `server/capture_liveness.py`).
 */
const IDLE_CLOSE_MARK = "(idle)";

/** The backend ended the session because no capture client was sending to it (#425). */
const ENDED_IDLE: Blocking = {
  message:
    "La sesión terminó por inactividad: ningún dispositivo estaba enviando datos. Empieza o reanuda una sesión para seguir.",
};

const CAPTURE_FAILURE = "No se han podido tomar las fotografías";
const UPLOAD_FAILURE = "El servidor no ha guardado las fotografías";
const END_FAILURE = "No se ha podido terminar la sesión";

/** Which of the two things the student photographs is being photographed now. */
const SOURCE_LABELS: Array<{ source: SourceKind; label: string }> = [
  { source: "book", label: "Libro" },
  { source: "notes", label: "Apuntes" },
];

/**
 * A browser refuses `getUserMedia` and the Web Speech API to any origin it does not call secure,
 * which is every address of this PC but `localhost`. jsdom has no `isSecureContext` at all, and a
 * missing answer is not a refusal.
 */
function insecureOrigin(): boolean {
  return window.isSecureContext === false;
}

/**
 * The Spanish sentence for a camera that refused. Read off the problem's own `code` and not off its
 * class: `Camera` reports a camera that went away by itself as a plain problem, and only a refusal
 * of `start()` or of a still arrives as a `CameraError`. A code this page does not know is a camera
 * that would not work, which is the one sentence that is always true.
 */
function cameraMessage(problem: unknown): string {
  return messageOf(CAMERA_MESSAGES, problem);
}

/** The same, for a recognizer that refused or gave up. */
function transcriberMessage(problem: unknown): string {
  return messageOf(TRANSCRIBER_MESSAGES, problem);
}

function messageOf<Code extends string>(
  messages: Record<Code, string>,
  problem: unknown,
): string {
  const code = (problem as { code?: unknown } | null)?.code;
  return typeof code === "string" && code in messages
    ? messages[code as Code]
    : (messages as Record<string, string>).unavailable;
}

/**
 * What `hello` announces. The audio format is promised only when this browser can really stream
 * it: a backend that read it would answer `stt_mode: "server"` and a client that cannot obey would
 * be a session with no transcription at all.
 */
export function captureCapabilities(): ClientCapabilities {
  if (audioStreamSupported()) return CAPTURE_CAPABILITIES;
  return { stt: "client", stt_provider: WEB_SPEECH_PROVIDER };
}

/** One utterance of the transcript the backend sends back, keyed by the id that replaces it. */
interface LiveSegment {
  readonly segmentId: string;
  readonly text: string;
  readonly final: boolean;
}

/**
 * The transcript with one segment of it merged in: a final replaces the partials that share its
 * `segment_id`, which is what keeps an utterance from being read twice while it is still growing.
 */
function mergeSegment(
  segments: readonly LiveSegment[],
  event: TranscriptPartial | TranscriptFinal,
): LiveSegment[] {
  const final = event.type === "transcript.final";
  const index = segments.findIndex((segment) => segment.segmentId === event.segment_id);
  if (index < 0) {
    return [...segments, { segmentId: event.segment_id, text: event.text, final }];
  }
  const merged = [...segments];
  const previous = merged[index];
  merged[index] = {
    segmentId: previous.segmentId,
    text: event.text,
    // A settled utterance stays settled: a partial the recognizer delivers late cannot undo it.
    final: final || previous.final,
  };
  return merged;
}

/** One burst in the strip: its photographs, how the upload is going and its thumbnail. */
interface BurstEntry {
  readonly key: number;
  /** The idempotency key `camera.ts` minted; null for a burst the camera never took. */
  readonly captureId: string | null;
  readonly state: BurstState;
  /** An object URL of the burst's first still, or null where the browser gives none. */
  readonly thumb: string | null;
}

/**
 * The strip's thumbnail of a burst. Object URLs are a browser's and jsdom has none, and a burst
 * whose stills failed has nothing to show either; both leave the state word alone in the strip.
 */
function thumbnailOf(blob: Blob | null): string | null {
  if (blob === null) return null;
  try {
    return URL.createObjectURL(blob);
  } catch {
    return null;
  }
}

/**
 * How one upload answer reads on the strip, and what the student is told when it failed. A failure
 * of the network or the server's side (#411) is worth sending again: the burst keeps its
 * `capture_id`, so a retry of one the backend did store is a duplicate, never a second copy. So is
 * a 409 while the connection is down, which is a backend back from a restart that has not had the
 * session resumed yet. Any other refusal is final.
 */
function uploadOutcome(
  result: CapturesResult,
  offline: boolean,
): { state: BurstState; trouble: string | null } {
  if (result.kind === "ok") {
    return {
      state: result.value.status === "duplicate" ? "duplicada" : "guardada",
      trouble: null,
    };
  }
  const retriable =
    result.kind === "unreachable" ||
    ((result.kind === "error" || result.kind === "refused") &&
      (result.status >= 500 || (offline && result.status === 409)));
  if (retriable) return { state: "pendiente", trouble: null };
  return { state: "error", trouble: describeFailure(UPLOAD_FAILURE, result) };
}

/**
 * The resume a reconnect asks for first, read as the socket needs it: a backend that is not there
 * (or not well) is asked again later, and a refusal means the session is over -- it ended, or
 * another session is open instead (protocol/README.md, "Session lifecycle").
 */
async function resumeOutcome(
  sessionId: string,
  onResumed: (receivedCaptureIds: readonly string[]) => void,
): Promise<ResumeOutcome> {
  const result = await resumeSession(sessionId);
  switch (result.kind) {
    case "ok":
      onResumed(result.value.received_capture_ids ?? []);
      return { kind: "ok" };
    case "refused":
      return result.status >= 500 ? { kind: "retry" } : { kind: "ended", detail: result.detail };
    case "error":
      return result.status >= 500 ? { kind: "retry" } : { kind: "ended" };
    case "unexpected":
    case "unreachable":
      return { kind: "retry" };
  }
}

/**
 * The click of the shutter, so a burst is something the student hears as well as sees. It is a
 * Web Audio burst and not a file: there is nothing to serve, and the page has no assets of its own.
 * A browser without Web Audio gets silence, which costs the click and nothing else.
 */
export function shutterClick(): void {
  const scope = window as unknown as {
    AudioContext?: typeof AudioContext;
    webkitAudioContext?: typeof AudioContext;
  };
  const Constructor = scope.AudioContext ?? scope.webkitAudioContext;
  if (Constructor === undefined) return;
  try {
    const context = new Constructor();
    // A context built before the student's first gesture starts suspended; a `capture_now` from
    // the backend is not a gesture, so the click asks it to run every time.
    void context.resume();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const startedAt = context.currentTime;
    oscillator.type = "square";
    oscillator.frequency.value = 1760;
    gain.gain.setValueAtTime(0.0001, startedAt);
    gain.gain.exponentialRampToValueAtTime(0.2, startedAt + 0.004);
    gain.gain.exponentialRampToValueAtTime(0.0001, startedAt + 0.05);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.onended = () => {
      oscillator.disconnect();
      gain.disconnect();
      void context.close();
    };
    oscillator.start(startedAt);
    oscillator.stop(startedAt + 0.06);
  } catch {
    // No click: a browser that will not give an audio graph still takes and uploads the burst.
  }
}

export interface CaptureScreenProps {
  /** The session the picker opened: its `ws_path` is what the socket dials and its id what the REST calls name. */
  session: Session;
  /** The names the picker showed the session under; a session never changes topic. */
  subjectName: string;
  topicName: string;
  /** Called once the backend ended the session, so `/capture` can go back to the picker. */
  onEnded?: (ended: SessionEndResponse) => void;
  /** The client clock every `client_time_ms` of this session is read from. */
  now?: () => number;
  /** The click of a burst; `shutterClick` by default, a spy in a test. */
  playShutter?: () => void;
  /** How long a burst's flash covers the preview. */
  flashMs?: number;
  /**
   * The screen is the study workspace's Captura tab: since #411 a session that ended elsewhere
   * points to Construir and Estudiar instead of the list of sessions, and since #413 the screen is
   * a section with an `h2` (the workspace has the page's `main` and `h1`) and has no doubts line
   * of its own (the doubts are asked in the workspace chat). Since #470 it is only the camera
   * preview, fitted to the tab without scrolling, and a bottom toolbar with one camera icon
   * («Capturar página»); a notice shows over the preview only while something is wrong.
   */
  embedded?: boolean;
  /**
   * Since #470: called with true while the capture is recording (connected, not paused, not
   * ending) and false otherwise, so the host can show a recording icon.
   */
  onRecordingChange?: (recording: boolean) => void;
  /** How long an outage lasts before the blocking message; `LONG_OUTAGE_MS` by default. */
  longOutageMs?: number;
  /** The reconnect backoff of the session socket; its own default when left out. */
  reconnectDelaysMs?: readonly number[];
  /**
   * Since #450: the host hides the screen (the workspace shows **Recursos**), so the capture pauses
   * as in a hidden browser tab (#425) -- camera and microphone stop, the socket says `pause` -- and
   * resumes when it turns false again.
   */
  suspended?: boolean;
}

export default function CaptureScreen({
  session,
  subjectName,
  topicName,
  onEnded,
  now = Date.now,
  playShutter = shutterClick,
  flashMs = FLASH_MS,
  embedded = false,
  longOutageMs = LONG_OUTAGE_MS,
  reconnectDelaysMs,
  suspended = false,
  onRecordingChange,
}: CaptureScreenProps) {
  const preview = useRef<HTMLVideoElement | null>(null);
  /** The three objects of a running session, so a press reaches the ones the effect built. */
  const runtime = useRef<{
    socket: SessionSocket | null;
    camera: Camera | null;
    transcriber: ClientTranscriber | null;
    wakeLock: ScreenWakeLock | null;
  }>({ socket: null, camera: null, transcriber: null, wakeLock: null });
  /** True once this screen stopped the session itself: its own close is not a lost connection. */
  const stopped = useRef(false);
  /**
   * True while this screen's end request is in flight (#319): the backend ends the session and
   * closes the socket before it answers, so a close then is the end of the session, not a loss.
   * `closedWhileEnding` keeps it in case the end fails after all.
   */
  const endingRef = useRef(false);
  const closedWhileEnding = useRef<string | null | undefined>(undefined);
  const burstKeys = useRef(0);
  const thumbs = useRef<string[]>([]);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  /**
   * The clock, read through a ref so that the session effect does not depend on it: a caller that
   * passes `now` as an inline arrow would otherwise hand the effect a new identity every render and
   * reopen a session that is already running.
   */
  const clock = useRef(now);
  clock.current = now;
  const reconnectDelays = useRef(reconnectDelaysMs);
  reconnectDelays.current = reconnectDelaysMs;
  /**
   * The session's latest vocabulary hints (protocol 1.4, #227): `hello.ack`'s list, replaced by
   * every `notice` that carries one. A ref, because a notice can arrive before the transcriber
   * exists (the camera is still starting) and the transcriber must start with the latest list.
   * Null until either of them set it, so a notice handled before the `hello.ack` continuation ran
   * is not overwritten by the older list of the ack.
   */
  const vocabularyHints = useRef<readonly string[] | null>(null);
  /** Since #411: what the socket's events need of the props, read when the event comes. */
  const settings = useRef({ embedded, longOutageMs });
  settings.current = { embedded, longOutageMs };
  /** The timer of the current outage's blocking message, and of «Conexión recuperada». */
  const outageTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const recoveredTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const uploadRetryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** The stills of every burst still to be stored, by key, so a retry sends the same burst. */
  const unsent = useRef(new Map<number, CapturedBurst>());
  /** The captures the last resume said the backend already has. */
  const receivedCaptures = useRef<ReadonlySet<string>>(new Set());

  const [blocking, setBlocking] = useState<Blocking | null>(null);
  const [trouble, setTrouble] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [sttMode, setSttMode] = useState<"client" | "server" | null>(null);
  const [cameraOn, setCameraOn] = useState(false);
  const [source, setSource] = useState<SourceKind | null>(null);
  const [pending, setPending] = useState<number | null>(null);
  /** Since 1.5 (#222): what the backend's own recognizer said went wrong; null while it works. */
  const [sttWarning, setSttWarning] = useState<string | null>(null);
  const [segments, setSegments] = useState<readonly LiveSegment[]>([]);
  const [bursts, setBursts] = useState<readonly BurstEntry[]>([]);
  const [flashing, setFlashing] = useState(false);
  const [ending, setEnding] = useState(false);
  /** Since #411: «Conexión recuperada» shows for a moment after a reconnect. */
  const [recovered, setRecovered] = useState(false);
  /**
   * Since #256: the Spanish sentence of a camera whose track ended mid-session, shown with the
   * **Reactivar cámara** button; null while the camera is fine or never started.
   */
  const [cameraLost, setCameraLost] = useState<string | null>(null);
  const [reactivating, setReactivating] = useState(false);
  /** Since #425: the tab is hidden, so the capture is paused (nothing is sent). */
  const [paused, setPaused] = useState(false);
  /** The same, for the socket's events and the device start, which run outside a render. */
  const pausedRef = useRef(false);
  /**
   * Why it is paused, as the backend was told (#454): `hidden` (a hidden tab: the backend's idle
   * auto-end applies) or `student` (the Recursos tab: the page is still in front of the student).
   */
  const pauseReasonRef = useRef<PauseReason | null>(null);
  /** Since #450: the host's `suspended`, read by the running effect. */
  const suspendedRef = useRef(suspended);
  suspendedRef.current = suspended;
  /** The running effect's pause/resume, called again when `suspended` changes. */
  const syncPause = useRef<(() => void) | null>(null);
  /**
   * Since #447: the running effect's answer to a reconnect's `hello.ack`, which may carry another
   * STT mode than the one the devices run by (the backend restarted with another config).
   */
  const followAck = useRef<((ack: HelloAck) => void) | null>(null);

  const flash = useCallback(() => {
    if (flashTimer.current !== null) clearTimeout(flashTimer.current);
    setFlashing(true);
    flashTimer.current = setTimeout(() => {
      flashTimer.current = null;
      setFlashing(false);
    }, flashMs);
  }, [flashMs]);

  const addBurst = useCallback((entry: Omit<BurstEntry, "key">): number => {
    burstKeys.current += 1;
    const key = burstKeys.current;
    if (entry.thumb !== null) thumbs.current.push(entry.thumb);
    setBursts((current) => [...current, { ...entry, key }]);
    return key;
  }, []);

  const setBurstState = useCallback((key: number, state: BurstState) => {
    setBursts((current) =>
      current.map((entry) => (entry.key === key ? { ...entry, state } : entry)),
    );
  }, []);

  /**
   * One burst, from the student's button or from a backend `capture_now`: three stills, the click
   * and the flash of a shutter, a thumbnail that says the upload is on its way, and the multipart
   * post that settles it. Whatever happens, a command is answered with its `ack`, because a
   * backend left without one waits for a client that has already given up (ADR-0006).
   */
  const captureBurst = useCallback(
    async (trigger: BurstTrigger) => {
      const camera = runtime.current.camera;
      const socket = runtime.current.socket;
      if (camera === null || socket === null) return;
      let burst: CapturedBurst;
      try {
        burst = await camera.takeBurst(trigger);
      } catch (problem) {
        const message = cameraMessage(problem);
        setTrouble(`${CAPTURE_FAILURE}: ${message}`);
        addBurst({ captureId: null, state: "error", thumb: null });
        if (trigger.trigger === "command") socket.sendAck(trigger.commandId, now());
        return;
      }
      playShutter();
      flash();
      const key = addBurst({
        captureId: burst.metadata.capture_id,
        state: "subiendo",
        thumb: thumbnailOf(burst.images.at(0)?.blob ?? null),
      });
      unsent.current.set(key, burst);
      await upload(key, burst);
      if (trigger.trigger === "command") socket.sendAck(trigger.commandId, now());
    },
    [addBurst, flash, now, playShutter, session.session_id, setBurstState],
  );

  /**
   * One upload of a burst, the first or a retry, and what its answer means for the strip. A
   * retriable failure keeps the burst for the next retry: after the reconnect when the connection
   * is down, a little later when it is up.
   */
  async function upload(key: number, burst: CapturedBurst): Promise<void> {
    const result = await uploadCaptures(session.session_id, burst.metadata, burst.images);
    const outcome = uploadOutcome(result, runtime.current.socket?.reconnecting ?? false);
    setBurstState(key, outcome.state);
    if (outcome.state !== "pendiente") unsent.current.delete(key);
    if (outcome.trouble !== null) setTrouble(outcome.trouble);
    if (outcome.state === "pendiente" && !(runtime.current.socket?.reconnecting ?? true)) {
      if (uploadRetryTimer.current === null) {
        uploadRetryTimer.current = setTimeout(() => {
          uploadRetryTimer.current = null;
          void actions.current.retryUploads();
        }, UPLOAD_RETRY_MS);
      }
    }
  }

  /**
   * Sends every pending burst again, except those the last resume said are stored already, which
   * simply become stored.
   */
  async function retryUploads(): Promise<void> {
    const received = receivedCaptures.current;
    const retries: Array<Promise<void>> = [];
    for (const [key, burst] of unsent.current) {
      if (burstsRef.current.find((entry) => entry.key === key)?.state !== "pendiente") continue;
      if (received.has(burst.metadata.capture_id)) {
        unsent.current.delete(key);
        setBurstState(key, "guardada");
        continue;
      }
      setBurstState(key, "subiendo");
      retries.push(upload(key, burst));
    }
    await Promise.all(retries);
  }

  /**
   * The socket's events for one whole session. It is built once, so what it calls has to be read
   * through the ref that every render refreshes instead of closed over.
   */
  const actions = useRef({ captureBurst, retryUploads });
  actions.current = { captureBurst, retryUploads };
  /** The strip as last rendered, for the retry and the leave warning, which run outside a render. */
  const burstsRef = useRef(bursts);
  burstsRef.current = bursts;

  const clearOutage = useCallback(() => {
    if (outageTimer.current !== null) clearTimeout(outageTimer.current);
    outageTimer.current = null;
  }, []);

  const onSocketEvent = useCallback((event: SessionSocketEvent) => {
    switch (event.kind) {
      case "transcript":
        setSegments((current) => mergeSegment(current, event.event));
        break;
      case "command":
        // `capture_now` is the only command of protocol v1, so there is nothing else to dispatch.
        void actions.current.captureBurst({
          trigger: "command",
          commandId: event.event.command_id,
        });
        break;
      case "notice":
        setPending(event.event.pending_count);
        // A notice without the field leaves the list as it is; one with it replaces the whole list.
        if (event.event.vocabulary_hints !== undefined) {
          vocabularyHints.current = event.event.vocabulary_hints;
          runtime.current.transcriber?.setVocabularyHints?.(event.event.vocabulary_hints);
        }
        break;
      case "sttStatus":
        setSttWarning(
          event.event.state === "ok" ? null : (event.event.detail ?? STT_DEGRADED[event.event.state]),
        );
        break;
      case "ack":
        setBursts((current) =>
          current.map((entry) =>
            entry.state === "subiendo" &&
            entry.captureId !== null &&
            (event.event.capture_ids ?? []).includes(entry.captureId)
              ? { ...entry, state: "guardada" }
              : entry,
          ),
        );
        break;
      case "reconnecting":
        if (stopped.current) return;
        if (endingRef.current) {
          // The end is on its way: this drop is part of it, or is shown if the end fails.
          if (closedWhileEnding.current === undefined) closedWhileEnding.current = null;
          return;
        }
        // The camera, the recognizer and the wake lock go on: this is an outage, not the end.
        setConnection("reconnecting");
        setRecovered(false);
        if (outageTimer.current === null) {
          outageTimer.current = setTimeout(() => {
            outageTimer.current = null;
            if (stopped.current) return;
            setConnection("lost");
            setBlocking(DISCONNECTED);
          }, settings.current.longOutageMs);
        }
        break;
      case "reconnected":
        if (stopped.current) return;
        // A new connection counts as sending until it hears otherwise: a hidden tab says so again.
        if (pausedRef.current) runtime.current.socket?.sendPause(clock.current(), pauseReasonRef.current ?? "hidden");
        followAck.current?.(event.ack);
        clearOutage();
        setConnection("open");
        setBlocking((current) => (current === DISCONNECTED ? null : current));
        setRecovered(true);
        if (recoveredTimer.current !== null) clearTimeout(recoveredTimer.current);
        recoveredTimer.current = setTimeout(() => {
          recoveredTimer.current = null;
          setRecovered(false);
        }, RECOVERED_MS);
        void actions.current.retryUploads();
        break;
      case "closed":
      case "failed":
        if (stopped.current) return;
        if (endingRef.current) {
          // Only the first report counts: a failure is followed by its close.
          if (closedWhileEnding.current === undefined) {
            closedWhileEnding.current = event.kind === "failed" ? event.problem : null;
          }
          return;
        }
        clearOutage();
        setRecovered(false);
        runtime.current.wakeLock?.stop();
        if (event.kind === "closed" && event.code === SESSION_NOT_ACTIVE_CLOSE) {
          setConnection("ended");
          const ended = event.reason.endsWith(IDLE_CLOSE_MARK)
            ? ENDED_IDLE
            : settings.current.embedded
              ? ENDED_ELSEWHERE_EMBEDDED
              : ENDED_ELSEWHERE;
          setBlocking({ ...ended, detail: event.reason || undefined });
          return;
        }
        setConnection("lost");
        setBlocking({
          ...DISCONNECTED,
          detail: event.kind === "failed" ? event.problem : undefined,
        });
        break;
      case "rejected":
        setBlocking({ ...PROTOCOL_REFUSED, detail: event.problem });
        break;
    }
  }, [clearOutage]);

  useEffect(() => {
    if (insecureOrigin()) {
      setConnection("unavailable");
      setBlocking(INSECURE);
      return;
    }
    stopped.current = false;
    const camera = new Camera({
      onLost: (problem) => {
        setCameraOn(false);
        // Only the camera is gone: the socket, the transcript and the uploaded bursts carry on.
        setCameraLost(cameraMessage(problem));
      },
    });
    const wakeLock = new ScreenWakeLock();
    wakeLock.start();
    /** The `hello.ack` the devices start by, once the handshake answered. */
    let acknowledged: HelloAck | null = null;
    /** Bumped by every stop of the devices, so a start still awaiting the camera gives up. */
    let devices = 0;
    const stopDevices = (): void => {
      devices += 1;
      runtime.current.transcriber?.stop();
      runtime.current.transcriber = null;
      camera.stop();
      setCameraOn(false);
    };
    /** The camera, then the recognizer or the audio stream of the connection's STT mode. */
    const startDevices = async (): Promise<void> => {
      const run = devices;
      const current = (): boolean => !disposed && run === devices && !pausedRef.current;
      try {
        await camera.start(preview.current);
        if (current()) setCameraOn(true);
      } catch (problem) {
        if (current()) setTrouble(cameraMessage(problem));
      }
      if (!current()) return;
      await startTranscriber();
    };
    /**
     * The recognizer (client mode) or the audio stream (server mode) of the latest `hello.ack`:
     * read when it starts, so a start still awaiting the camera follows a mode that changed.
     */
    const startTranscriber = async (): Promise<void> => {
      const ack = acknowledged;
      if (ack === null) return;
      const transcriber: ClientTranscriber =
        ack.stt_mode === "server"
          ? new AudioStreamTranscriber(
              {
                onSegment: () => {
                  // The backend transcribes the audio itself and sends the transcript back.
                },
                onProblem: (problem) => {
                  if (!disposed) setTrouble(transcriberMessage(problem));
                },
              },
              { sink: socket, audioFormat: ack.audio_format ?? undefined },
            )
          : new WebSpeechTranscriber(
              {
                onSegment: (segment, kind) => socket.sendTranscript(segment, kind),
                onProblem: (problem) => {
                  if (!disposed) setTrouble(transcriberMessage(problem));
                },
              },
              { vocabularyHints: vocabularyHints.current ?? [] },
            );
      runtime.current.transcriber = transcriber;
      try {
        await transcriber.start();
      } catch (problem) {
        if (!disposed) setTrouble(transcriberMessage(problem));
      }
    };
    // Since #425 the capture runs only while the tab is visible: hidden, nothing is sent and the
    // backend is told `pause`; visible again, `resume` and the devices start again. Since #450 the
    // host's `suspended` (the workspace's Recursos tab) pauses it the same way.
    // Since #454 the backend is told why: a hidden tab (`hidden`, which wins) stops sending and
    // the idle auto-end applies; the Recursos tab (`student`) keeps the session. A change between
    // the two says `pause` again with the new reason.
    const pauseReason = (): PauseReason | null =>
      document.visibilityState === "hidden" ? "hidden" : suspendedRef.current ? "student" : null;
    const onVisibilityChange = (): void => {
      if (stopped.current) return;
      const reason = pauseReason();
      if (reason === pauseReasonRef.current) return;
      const wasPaused = pausedRef.current;
      pauseReasonRef.current = reason;
      pausedRef.current = reason !== null;
      setPaused(reason !== null);
      if (reason !== null) {
        if (!wasPaused) stopDevices();
        socket.sendPause(clock.current(), reason);
        return;
      }
      socket.sendButton("resume", clock.current());
      setCameraLost(null);
      if (acknowledged !== null) void startDevices();
    };
    // Since #447 a reconnect may land on a backend that restarted in the other STT mode: the
    // running transcriber would send frames that backend refuses (1008), so it gives way to the
    // one of the new mode. The camera goes on. A hidden tab starts nothing (its devices are
    // stopped); the visible tab starts the new mode's transcriber. A start still awaiting the
    // camera reads the new ack itself.
    followAck.current = (ack: HelloAck): void => {
      const previous = acknowledged;
      acknowledged = ack;
      if (previous === null || previous.stt_mode === ack.stt_mode) return;
      setSttMode(ack.stt_mode);
      setSttWarning(null);
      const running = runtime.current.transcriber;
      if (running === null) return;
      running.stop();
      runtime.current.transcriber = null;
      if (!pausedRef.current) void startTranscriber();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    syncPause.current = onVisibilityChange;
    // Leaving the page while a burst or a queued frame has not reached the backend loses it, so
    // the browser asks first -- then and only then.
    const onBeforeUnload = (event: BeforeUnloadEvent): void => {
      if (stopped.current) return;
      const uploading = burstsRef.current.some(
        (entry) => entry.state === "subiendo" || entry.state === "pendiente",
      );
      const queued = (runtime.current.socket?.queuedCount ?? 0) > 0;
      if (!uploading && !queued) return;
      event.preventDefault();
      // Older Chrome and Edge only ask when `returnValue` is set.
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    receivedCaptures.current = new Set();
    const socket = new SessionSocket({
      wsPath: session.ws_path,
      clientTimeMs: clock.current(),
      capabilities: captureCapabilities(),
      onEvent: onSocketEvent,
      reconnect: {
        resume: () =>
          resumeOutcome(session.session_id, (received) => {
            receivedCaptures.current = new Set(received);
          }),
        clock: () => clock.current(),
        delaysMs: reconnectDelays.current,
      },
    });
    runtime.current = { socket, camera, transcriber: null, wakeLock };
    vocabularyHints.current = null;

    let disposed = false;
    pauseReasonRef.current = pauseReason();
    pausedRef.current = pauseReasonRef.current !== null;
    setPaused(pausedRef.current);
    // A screen opened in a hidden (or suspended) tab says so right after its `hello` (a bare
    // `pause`: the version that takes its reason is not negotiated yet).
    if (pauseReasonRef.current !== null) socket.sendPause(clock.current(), pauseReasonRef.current);
    void (async () => {
      const handshake = await socket.handshake;
      if (disposed) return;
      if (handshake.kind !== "ok") {
        wakeLock.stop();
        setConnection("lost");
        setBlocking(
          handshake.kind === "rejected"
            ? { ...HANDSHAKE_REFUSED, detail: handshake.problem }
            : { ...HANDSHAKE_LOST, detail: handshake.problem },
        );
        return;
      }
      setConnection("open");
      setSttMode(handshake.ack.stt_mode);
      vocabularyHints.current ??= handshake.ack.vocabulary_hints ?? [];
      acknowledged = handshake.ack;
      // Now the reason can go out: a screen opened on the Recursos tab says it is only set aside.
      if (pauseReasonRef.current === "student") socket.sendPause(clock.current(), "student");
      if (!pausedRef.current) await startDevices();
    })();

    return () => {
      disposed = true;
      stopped.current = true;
      followAck.current = null;
      syncPause.current = null;
      runtime.current.transcriber?.stop();
      runtime.current = { socket: null, camera: null, transcriber: null, wakeLock: null };
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("beforeunload", onBeforeUnload);
      clearOutage();
      wakeLock.stop();
      camera.stop();
      socket.close();
    };
  }, [clearOutage, onSocketEvent, session.session_id, session.ws_path]);

  // Since #450: the host hid the screen (or showed it again); pause or resume as a hidden tab does.
  useEffect(() => {
    syncPause.current?.();
  }, [suspended]);

  // Object URLs are the browser's to give back, and a strip of a hundred bursts is a hundred of them.
  useEffect(
    () => () => {
      if (flashTimer.current !== null) clearTimeout(flashTimer.current);
      if (recoveredTimer.current !== null) clearTimeout(recoveredTimer.current);
      if (uploadRetryTimer.current !== null) clearTimeout(uploadRetryTimer.current);
      for (const thumb of thumbs.current) {
        try {
          URL.revokeObjectURL(thumb);
        } catch {
          // A browser that gave no object URL has none to take back.
        }
      }
      thumbs.current = [];
    },
    [],
  );

  const signal = useCallback(
    (button: "important" | "switch_source", source?: SourceKind) => {
      runtime.current.socket?.sendButton(button, now(), source);
    },
    [now],
  );

  const chooseSource = useCallback(
    (chosen: SourceKind) => {
      setSource(chosen);
      signal("switch_source", chosen);
    },
    [signal],
  );

  /**
   * Reactivar cámara (#256): asks the browser for the camera again after its track ended, on the
   * same preview. A refusal keeps the notice up with its own sentence, so the student can fix what
   * it says and press again; nothing else of the session is touched either way.
   */
  async function reactivateCamera() {
    const camera = runtime.current.camera;
    if (camera === null || reactivating) return;
    setReactivating(true);
    try {
      await camera.start(preview.current);
      if (runtime.current.camera !== camera) return;
      setCameraOn(true);
      setCameraLost(null);
    } catch (problem) {
      if (runtime.current.camera === camera) setCameraLost(cameraMessage(problem));
    } finally {
      setReactivating(false);
    }
  }

  /**
   * Terminar: the session ends on the backend first and the devices are given back after, so a
   * refusal of the end is a refusal the student still reads with the session on the page. Either
   * way the camera, the microphone and the socket stop: an end that failed is not a reason to keep
   * a laptop's light on. There is no "Terminar y preparar apuntes" on the web since #413 (it stays
   * on the Android app): the whole topic is prepared by asking the workspace chat for it.
   */
  async function finish() {
    setEnding(true);
    endingRef.current = true;
    closedWhileEnding.current = undefined;
    const result = await endSession(session.session_id, "button", now());
    endingRef.current = false;
    stopped.current = true;
    runtime.current.wakeLock?.stop();
    runtime.current.transcriber?.stop();
    runtime.current.transcriber = null;
    runtime.current.camera?.stop();
    setCameraOn(false);
    setCameraLost(null);
    runtime.current.socket?.close();
    runtime.current.socket = null;
    if (result.kind === "ok") {
      onEnded?.(result.value);
      return;
    }
    setEnding(false);
    setTrouble(describeFailure(END_FAILURE, result));
    // The end failed, so a close that came meanwhile was a lost connection after all.
    if (closedWhileEnding.current !== undefined) {
      setConnection("lost");
      setBlocking({ ...DISCONNECTED, detail: closedWhileEnding.current ?? undefined });
    }
  }

  // An outage keeps the controls: a press waits for the resume and a burst for its retry.
  const live =
    (connection === "open" || connection === "reconnecting") && blocking === null && !ending;
  // Failures behind the scenes (#262): a discreet line each, only while something is wrong.
  const health = useSessionHealth(
    session.session_id,
    !ending && connection !== "lost" && connection !== "ended",
  );
  const pendingLine =
    pending === null
      ? "El servidor todavía no ha avisado de ninguna duda."
      : pending === 0
        ? "No hay dudas pendientes de revisar."
        : pending === 1
          ? "1 duda pendiente de revisar."
          : `${pending} dudas pendientes de revisar.`;
  const modeLine =
    sttMode === "client"
      ? "Transcribe este navegador."
      : sttMode === "server"
        ? "Transcribe el servidor: esta página le envía el audio del micrófono."
        : null;

  // Since #470 the host shows whether the capture is recording (the Captura tab's icon).
  const recording = live && !paused;
  useEffect(() => {
    onRecordingChange?.(recording);
  }, [recording, onRecordingChange]);

  if (embedded) {
    // Since #470 the workspace's Captura tab is the preview and one camera icon, nothing else: the
    // transcript, the photo strip, the headings, the status sentences and the other buttons go
    // (the voice commands still say «importante», «ahora el libro»…; the session ends when the
    // workspace is left). What is wrong still shows, compactly, over the preview; what is fine is
    // only said to a screen reader.
    const quietConnection = connection === "open" && !recovered;
    const waiting = bursts.filter((entry) => entry.state === "pendiente").length;
    const failed = bursts.filter((entry) => entry.state === "error").length;
    return (
      <section className="capture-page capture-screen capture-embedded" aria-label="Captura en curso">
        <h2 className="capture-sr-only">Capturar una sesión de estudio</h2>
        <div className="capture-stage">
          <video className="capture-preview" ref={preview} autoPlay playsInline muted aria-label="Vista previa de la cámara" />
          {flashing && <div className="capture-flash" data-testid="capture-flash" aria-hidden="true" />}
          <p className="capture-sr-only" role="status" aria-label="Estado de la cámara">
            {cameraOn ? "La cámara está en marcha." : "La cámara no está en marcha."}
          </p>
          <div className="capture-notices">
            <p
              className={`capture-notice capture-connection${quietConnection ? " capture-sr-only" : ""}`}
              data-connection={connection}
              role="status"
              aria-label="Estado de la conexión"
            >
              {connection === "open" && recovered ? "Conexión recuperada" : CONNECTION_TEXT[connection]}
              {connection === "open" && live && modeLine !== null ? `. ${modeLine}` : ""}
            </p>
            {blocking !== null && (
              <p className="capture-notice capture-notice-bad" role="alert" title={blocking.detail ?? blocking.message}>
                {blocking.message}
              </p>
            )}
            {trouble !== null && (
              <p className="capture-notice capture-notice-bad" role="alert" title={trouble}>
                {trouble}
              </p>
            )}
            {paused && !ending && connection !== "ended" && connection !== "lost" && (
              <p className="capture-notice" role="status" aria-label="Captura en pausa">
                {suspended ? SUSPENDED_NOTICE : PAUSED_NOTICE}
              </p>
            )}
            {sttWarning !== null && (
              <p className="capture-notice capture-notice-bad" role="alert" aria-label="Estado de la transcripción" title={sttWarning}>
                {sttWarning}
              </p>
            )}
            {cameraLost !== null && !ending && (
              <div className="capture-notice capture-notice-bad capture-notice-action" role="alert" aria-label="Cámara desconectada">
                <p title={cameraLost}>{cameraLost}</p>
                <button type="button" disabled={reactivating} onClick={() => void reactivateCamera()}>
                  {reactivating ? "Reactivando la cámara…" : "Reactivar cámara"}
                </button>
              </div>
            )}
            {health.length > 0 && (
              <ul className="capture-notice capture-health" role="status" aria-label="Estado del servidor">
                {health.map((line) => (
                  <li key={line} title={line}>
                    {line}
                  </li>
                ))}
              </ul>
            )}
            {(waiting > 0 || failed > 0) && (
              <p className="capture-notice" role="status" aria-label="Fotografías sin guardar">
                {[
                  waiting > 0 ? (waiting === 1 ? "1 foto pendiente de subir" : `${waiting} fotos pendientes de subir`) : null,
                  failed > 0 ? (failed === 1 ? "1 foto no se ha podido subir" : `${failed} fotos no se han podido subir`) : null,
                ]
                  .filter((part) => part !== null)
                  .join(" · ")}
              </p>
            )}
          </div>
        </div>
        <div className="capture-toolbar" role="group" aria-label="Controles de la sesión">
          <button
            type="button"
            className="capture-tool"
            aria-label="Capturar página"
            title="Capturar página"
            disabled={!live || !cameraOn}
            onClick={() => void captureBurst({ trigger: "button" })}
          >
            <CameraIcon />
          </button>
        </div>
      </section>
    );
  }

  return (
    <main className="capture-page capture-screen">
      <header className="capture-header">
        <h1>Capturar una sesión de estudio</h1>
        <p className="page-context">
          {subjectName} · {topicName}
        </p>
        <p className="capture-connection" data-connection={connection} role="status" aria-label="Estado de la conexión">
          {connection === "open" && recovered ? "Conexión recuperada" : CONNECTION_TEXT[connection]}
          {connection === "open" && live && modeLine !== null ? `. ${modeLine}` : ""}
        </p>
      </header>

      {blocking !== null && (
        <p role="alert" title={blocking.detail}>
          {blocking.message}
        </p>
      )}
      {trouble !== null && <p role="alert">{trouble}</p>}
      {paused && !ending && connection !== "ended" && connection !== "lost" && (
        <p role="status" aria-label="Captura en pausa">
          {suspended ? SUSPENDED_NOTICE : PAUSED_NOTICE}
        </p>
      )}
      {sttWarning !== null && (
        <p role="alert" aria-label="Estado de la transcripción">
          {sttWarning}
        </p>
      )}

      <section className="capture-camera" aria-label="Cámara">
        <h2>Cámara</h2>
        <video className="capture-preview" ref={preview} autoPlay playsInline muted aria-label="Vista previa de la cámara" />
        {flashing && <div className="capture-flash" data-testid="capture-flash" aria-hidden="true" />}
        <p className="capture-camera-status" role="status" aria-label="Estado de la cámara">
          {cameraOn ? "La cámara está en marcha." : "La cámara no está en marcha."}
        </p>
        {cameraLost !== null && !ending && (
          <div role="alert" aria-label="Cámara desconectada">
            <p>{cameraLost}</p>
            <button
              type="button"
              disabled={reactivating}
              onClick={() => void reactivateCamera()}
            >
              {reactivating ? "Reactivando la cámara…" : "Reactivar cámara"}
            </button>
          </div>
        )}
      </section>

      <section className="capture-controls" aria-label="Controles de la sesión">
        <h2>Controles</h2>
        <button
          type="button"
          className="capture-shutter"
          disabled={!live || !cameraOn}
          onClick={() => void captureBurst({ trigger: "button" })}
        >
          Capturar
        </button>
        <button type="button" className="capture-important" disabled={!live} onClick={() => signal("important")}>
          Importante
        </button>
        {SOURCE_LABELS.map((entry) => (
          <button
            key={entry.source}
            type="button"
            className="capture-source"
            aria-pressed={source === entry.source}
            disabled={!live}
            onClick={() => chooseSource(entry.source)}
          >
            {entry.label}
          </button>
        ))}
        <button type="button" className="capture-end" disabled={ending} onClick={() => void finish()}>
          {ending ? "Terminando la sesión…" : "Terminar"}
        </button>
      </section>

      {health.length > 0 && (
        <ul className="capture-health" role="status" aria-label="Estado del servidor">
          {health.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}

      <p className="capture-pending" role="status" aria-label="Dudas pendientes">
        {pendingLine}
      </p>

      <section className="capture-transcript" aria-label="Transcripción en directo">
        <h2>Transcripción</h2>
        {segments.length === 0 && <p>Todavía no se ha transcrito nada.</p>}
        {segments.length > 0 && (
          <ol aria-label="Segmentos transcritos">
            {segments.map((segment) => (
              <li key={segment.segmentId} className={segment.final ? FINAL_CLASS : PARTIAL_CLASS}>
                {segment.text}
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="capture-photos" aria-label="Fotografías de la sesión">
        <h2>Fotografías</h2>
        {bursts.length === 0 && <p>Todavía no has capturado ninguna página.</p>}
        {bursts.length > 0 && (
          <ul className="capture-bursts" aria-label="Ráfagas capturadas">
            {bursts.map((entry) => (
              <li key={entry.key} className={`capture-burst capture-burst-${entry.state}`}>
                {entry.thumb !== null && <img src={entry.thumb} alt={`Ráfaga ${entry.key}`} />}
                {BURST_LABELS[entry.state]}
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

/** The camera icon of the embedded toolbar's one button (#470). */
function CameraIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 8a2 2 0 0 1 2-2h2l1.5-2h5L16 6h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" />
      <circle cx="12" cy="12.5" r="3.5" />
    </svg>
  );
}
