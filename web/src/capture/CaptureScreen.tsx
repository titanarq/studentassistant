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
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ClientCapabilities,
  Session,
  SessionEndResponse,
  SourceKind,
  TranscriptFinal,
  TranscriptPartial,
} from "../protocol";
import { type CapturesResult, endSession, uploadCaptures } from "./api";
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
  SESSION_NOT_ACTIVE_CLOSE,
  SessionSocket,
  type SessionSocketEvent,
  WEB_SPEECH_PROVIDER,
} from "./sessionSocket";
import NotesProgress from "./NotesProgress";
import { type ClientTranscriber, type TranscriberProblemCode } from "./transcriber";
import { useSessionHealth } from "./sessionHealth";
import { ScreenWakeLock } from "./wakeLock";
import { WebSpeechTranscriber } from "./webSpeechTranscriber";
import "./capture.css";

/** How long a burst's flash covers the preview: long enough to see, short enough to not miss. */
export const FLASH_MS = 160;

/**
 * The partial's muted grey and the final's full ink colour, as classes of `capture.css` so both
 * follow the light and dark themes of the design system (#296).
 */
const PARTIAL_CLASS = "capture-segment capture-segment-partial";
const FINAL_CLASS = "capture-segment capture-segment-final";

/** How a burst's upload is going, in the words the strip shows. */
type BurstState = "subiendo" | "guardada" | "duplicada" | "error";

/**
 * The session's connection, in the four states the page shows: it is opening, the backend accepted
 * it, it ended by itself, or this page could never open one. `unavailable` stays apart from `lost`
 * because a student who loaded the page from an address the browser does not trust has nothing to
 * reconnect to.
 */
type ConnectionState = "connecting" | "open" | "lost" | "ended" | "unavailable";

const CONNECTION_TEXT: Record<ConnectionState, string> = {
  connecting: "Conectando con el servidor…",
  open: "Conectado con el servidor",
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
 * What the student reads on coming back to a tab that was hidden mid-session (#256): Chrome throttles
 * a background tab, and the recognizer of `client` mode can go quiet with it. It is a caution and
 * not a failure -- the session went on -- so it is a status line and it clears on the next final.
 */
const HIDDEN_TAB_NOTICE =
  "Mientras esta pestaña estaba oculta, la transcripción puede haberse pausado. Si has hablado entonces, repítelo.";

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

/** How one upload answer reads on the strip, and what the student is told when it failed. */
function uploadOutcome(result: CapturesResult): { state: BurstState; trouble: string | null } {
  if (result.kind === "ok") {
    return {
      state: result.value.status === "duplicate" ? "duplicada" : "guardada",
      trouble: null,
    };
  }
  return { state: "error", trouble: describeFailure(UPLOAD_FAILURE, result) };
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
  /** How often the notes generation is polled after "Terminar y preparar apuntes". */
  notesPollMs?: number;
}

export default function CaptureScreen({
  session,
  subjectName,
  topicName,
  onEnded,
  now = Date.now,
  playShutter = shutterClick,
  flashMs = FLASH_MS,
  notesPollMs,
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
  /**
   * The session's latest vocabulary hints (protocol 1.4, #227): `hello.ack`'s list, replaced by
   * every `notice` that carries one. A ref, because a notice can arrive before the transcriber
   * exists (the camera is still starting) and the transcriber must start with the latest list.
   * Null until either of them set it, so a notice handled before the `hello.ack` continuation ran
   * is not overwritten by the older list of the ack.
   */
  const vocabularyHints = useRef<readonly string[] | null>(null);

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
  /**
   * Since #256: the Spanish sentence of a camera whose track ended mid-session, shown with the
   * **Reactivar cámara** button; null while the camera is fine or never started.
   */
  const [cameraLost, setCameraLost] = useState<string | null>(null);
  const [reactivating, setReactivating] = useState(false);
  /** Since #256: the tab was hidden mid-session, so transcription may have paused meanwhile. */
  const [hiddenTabNotice, setHiddenTabNotice] = useState(false);
  /**
   * Since #271: the session ended with `prepare_notes`, so the screen gives way to the generation's
   * progress; `onEnded` waits for the student's "Volver" there.
   */
  const [prepared, setPrepared] = useState<SessionEndResponse | null>(null);

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
      const result = await uploadCaptures(session.session_id, burst.metadata, burst.images);
      const outcome = uploadOutcome(result);
      setBurstState(key, outcome.state);
      if (outcome.trouble !== null) setTrouble(outcome.trouble);
      if (trigger.trigger === "command") socket.sendAck(trigger.commandId, now());
    },
    [addBurst, flash, now, playShutter, session.session_id, setBurstState],
  );

  /**
   * The socket's events for one whole session. It is built once, so what it calls has to be read
   * through the ref that every render refreshes instead of closed over.
   */
  const actions = useRef({ captureBurst });
  actions.current = { captureBurst };

  const onSocketEvent = useCallback((event: SessionSocketEvent) => {
    switch (event.kind) {
      case "transcript":
        setSegments((current) => mergeSegment(current, event.event));
        // Text is flowing again, so whatever the hidden tab paused has resumed.
        if (event.event.type === "transcript.final") setHiddenTabNotice(false);
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
        runtime.current.wakeLock?.stop();
        if (event.kind === "closed" && event.code === SESSION_NOT_ACTIVE_CLOSE) {
          setConnection("ended");
          setBlocking({ ...ENDED_ELSEWHERE, detail: event.reason || undefined });
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
  }, []);

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
    let tabWasHidden = false;
    const onVisibilityChange = (): void => {
      if (stopped.current) return;
      if (document.visibilityState === "hidden") {
        tabWasHidden = true;
      } else if (tabWasHidden) {
        tabWasHidden = false;
        setHiddenTabNotice(true);
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    const socket = new SessionSocket({
      wsPath: session.ws_path,
      clientTimeMs: clock.current(),
      capabilities: captureCapabilities(),
      onEvent: onSocketEvent,
    });
    runtime.current = { socket, camera, transcriber: null, wakeLock };
    vocabularyHints.current = null;

    let disposed = false;
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
      try {
        await camera.start(preview.current);
        if (!disposed) setCameraOn(true);
      } catch (problem) {
        if (!disposed) setTrouble(cameraMessage(problem));
      }
      if (disposed) return;
      const transcriber: ClientTranscriber =
        handshake.ack.stt_mode === "server"
          ? new AudioStreamTranscriber(
              {
                onSegment: () => {
                  // The backend transcribes the audio itself and sends the transcript back.
                },
                onProblem: (problem) => {
                  if (!disposed) setTrouble(transcriberMessage(problem));
                },
              },
              { sink: socket, audioFormat: handshake.ack.audio_format ?? undefined },
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
    })();

    return () => {
      disposed = true;
      stopped.current = true;
      runtime.current.transcriber?.stop();
      runtime.current = { socket: null, camera: null, transcriber: null, wakeLock: null };
      document.removeEventListener("visibilitychange", onVisibilityChange);
      wakeLock.stop();
      camera.stop();
      socket.close();
    };
  }, [onSocketEvent, session.ws_path]);

  // Object URLs are the browser's to give back, and a strip of a hundred bursts is a hundred of them.
  useEffect(
    () => () => {
      if (flashTimer.current !== null) clearTimeout(flashTimer.current);
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
   * a laptop's light on. "Terminar y preparar apuntes" (#271, `prepareNotes`) is the same end with
   * `prepare_notes: true`, after which the screen follows the notes generation instead of leaving.
   */
  async function finish(prepareNotes = false) {
    setEnding(true);
    endingRef.current = true;
    closedWhileEnding.current = undefined;
    const result = await endSession(session.session_id, "button", now(), prepareNotes);
    endingRef.current = false;
    stopped.current = true;
    runtime.current.wakeLock?.stop();
    runtime.current.transcriber?.stop();
    runtime.current.transcriber = null;
    runtime.current.camera?.stop();
    setCameraOn(false);
    setCameraLost(null);
    setHiddenTabNotice(false);
    runtime.current.socket?.close();
    runtime.current.socket = null;
    if (result.kind === "ok") {
      if (prepareNotes) setPrepared(result.value);
      else onEnded?.(result.value);
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

  const live = connection === "open" && blocking === null && !ending;
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

  if (prepared !== null) {
    return (
      <NotesProgress
        subjectId={session.subject_id}
        topicId={session.topic_id}
        subjectName={subjectName}
        topicName={topicName}
        start={prepared.notes_generation}
        onClose={() => onEnded?.(prepared)}
        intervalMs={notesPollMs}
      />
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
          {CONNECTION_TEXT[connection]}
          {live && modeLine !== null ? `. ${modeLine}` : ""}
        </p>
      </header>

      {blocking !== null && (
        <p role="alert" title={blocking.detail}>
          {blocking.message}
        </p>
      )}
      {trouble !== null && <p role="alert">{trouble}</p>}
      {hiddenTabNotice && (
        <p role="status" aria-label="Aviso de pestaña oculta">
          {HIDDEN_TAB_NOTICE}
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
        <button type="button" className="capture-end" disabled={ending} onClick={() => void finish(true)}>
          Terminar y preparar apuntes
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
