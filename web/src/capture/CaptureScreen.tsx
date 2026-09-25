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
  SessionSocket,
  type SessionSocketEvent,
  WEB_SPEECH_PROVIDER,
} from "./sessionSocket";
import { type ClientTranscriber, type TranscriberProblemCode } from "./transcriber";
import { WebSpeechTranscriber } from "./webSpeechTranscriber";

/** How long a burst's flash covers the preview: long enough to see, short enough to not miss. */
export const FLASH_MS = 160;

/**
 * The partial's grey and the final's near-black. The app has no stylesheet yet, so the two colours
 * the transcript is specified in are carried by the elements themselves instead of a class a
 * stylesheet nobody wrote would have to define.
 */
const PARTIAL_STYLE = { color: "#5c5c5c" } as const;
const FINAL_STYLE = { color: "#111111" } as const;

/** How a burst's upload is going, in the words the strip shows. */
type BurstState = "subiendo" | "guardada" | "duplicada" | "error";

/**
 * The session's connection, in the four states the page shows: it is opening, the backend accepted
 * it, it ended by itself, or this page could never open one. `unavailable` stays apart from `lost`
 * because a student who loaded the page from an address the browser does not trust has nothing to
 * reconnect to.
 */
type ConnectionState = "connecting" | "open" | "lost" | "unavailable";

const CONNECTION_TEXT: Record<ConnectionState, string> = {
  connecting: "Conectando con el servidor…",
  open: "Conectado con el servidor",
  lost: "Se ha perdido la conexión con el servidor",
  unavailable: "Esta página no puede abrir la sesión",
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
  lost: "La cámara se ha desconectado o otra aplicación se la ha quedado. Vuelve a conectarla.",
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
}

export default function CaptureScreen({
  session,
  subjectName,
  topicName,
  onEnded,
  now = Date.now,
  playShutter = shutterClick,
  flashMs = FLASH_MS,
}: CaptureScreenProps) {
  const preview = useRef<HTMLVideoElement | null>(null);
  /** The three objects of a running session, so a press reaches the ones the effect built. */
  const runtime = useRef<{
    socket: SessionSocket | null;
    camera: Camera | null;
    transcriber: ClientTranscriber | null;
  }>({ socket: null, camera: null, transcriber: null });
  /** True once this screen stopped the session itself: its own close is not a lost connection. */
  const stopped = useRef(false);
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

  const [blocking, setBlocking] = useState<Blocking | null>(null);
  const [trouble, setTrouble] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [sttMode, setSttMode] = useState<"client" | "server" | null>(null);
  const [cameraOn, setCameraOn] = useState(false);
  const [source, setSource] = useState<SourceKind | null>(null);
  const [pending, setPending] = useState<number | null>(null);
  const [segments, setSegments] = useState<readonly LiveSegment[]>([]);
  const [bursts, setBursts] = useState<readonly BurstEntry[]>([]);
  const [flashing, setFlashing] = useState(false);
  const [ending, setEnding] = useState(false);

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
        setTrouble(cameraMessage(problem));
      },
    });
    const socket = new SessionSocket({
      wsPath: session.ws_path,
      clientTimeMs: clock.current(),
      capabilities: captureCapabilities(),
      onEvent: onSocketEvent,
    });
    runtime.current = { socket, camera, transcriber: null };

    let disposed = false;
    void (async () => {
      const handshake = await socket.handshake;
      if (disposed) return;
      if (handshake.kind !== "ok") {
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
          : new WebSpeechTranscriber({
              onSegment: (segment, kind) => socket.sendTranscript(segment, kind),
              onProblem: (problem) => {
                if (!disposed) setTrouble(transcriberMessage(problem));
              },
            });
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
      runtime.current = { socket: null, camera: null, transcriber: null };
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
   * Terminar: the session ends on the backend first and the devices are given back after, so a
   * refusal of the end is a refusal the student still reads with the session on the page. Either
   * way the camera, the microphone and the socket stop: an end that failed is not a reason to keep
   * a laptop's light on.
   */
  async function finish() {
    setEnding(true);
    const result = await endSession(session.session_id, "button", now());
    stopped.current = true;
    runtime.current.transcriber?.stop();
    runtime.current.transcriber = null;
    runtime.current.camera?.stop();
    setCameraOn(false);
    runtime.current.socket?.close();
    runtime.current.socket = null;
    if (result.kind === "ok") {
      onEnded?.(result.value);
      return;
    }
    setEnding(false);
    setTrouble(describeFailure(END_FAILURE, result));
  }

  const live = connection === "open" && blocking === null && !ending;
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

  return (
    <main>
      <header>
        <h1>Capturar una sesión de estudio</h1>
        <p>
          {subjectName} · {topicName}
        </p>
        <p role="status" aria-label="Estado de la conexión">
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

      <section aria-label="Cámara">
        <h2>Cámara</h2>
        <video ref={preview} autoPlay playsInline muted aria-label="Vista previa de la cámara" />
        {flashing && <div data-testid="capture-flash" aria-hidden="true" />}
        <p role="status" aria-label="Estado de la cámara">
          {cameraOn ? "La cámara está en marcha." : "La cámara no está en marcha."}
        </p>
      </section>

      <section aria-label="Controles de la sesión">
        <h2>Controles</h2>
        <button
          type="button"
          disabled={!live || !cameraOn}
          onClick={() => void captureBurst({ trigger: "button" })}
        >
          Capturar
        </button>
        <button type="button" disabled={!live} onClick={() => signal("important")}>
          Importante
        </button>
        {SOURCE_LABELS.map((entry) => (
          <button
            key={entry.source}
            type="button"
            aria-pressed={source === entry.source}
            disabled={!live}
            onClick={() => chooseSource(entry.source)}
          >
            {entry.label}
          </button>
        ))}
        <button type="button" disabled={ending} onClick={() => void finish()}>
          {ending ? "Terminando la sesión…" : "Terminar"}
        </button>
      </section>

      <p role="status" aria-label="Dudas pendientes">
        {pendingLine}
      </p>

      <section aria-label="Transcripción en directo">
        <h2>Transcripción</h2>
        {segments.length === 0 && <p>Todavía no se ha transcrito nada.</p>}
        {segments.length > 0 && (
          <ol aria-label="Segmentos transcritos">
            {segments.map((segment) => (
              <li key={segment.segmentId} style={segment.final ? FINAL_STYLE : PARTIAL_STYLE}>
                {segment.text}
              </li>
            ))}
          </ol>
        )}
      </section>

      <section aria-label="Fotografías de la sesión">
        <h2>Fotografías</h2>
        {bursts.length === 0 && <p>Todavía no has capturado ninguna página.</p>}
        {bursts.length > 0 && (
          <ul aria-label="Ráfagas capturadas">
            {bursts.map((entry) => (
              <li key={entry.key}>
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
