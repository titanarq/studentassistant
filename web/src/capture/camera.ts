/**
 * The camera the capture page previews and takes its page photos from (#40). Two things shape this
 * file. A still has to come out at the resolution the camera really offers and not at whatever the
 * preview stream settled on, so the track is opened asking for the largest frame there is and then,
 * where the browser has the Image Capture API, raised to the sensor's own maximum still size. And a
 * browser that has no `ImageCapture` -- Firefox has none -- gets the fallback that draws the
 * preview onto a canvas at the track's full settings: a smaller photo of the same page, which beats
 * no photo. One burst is three stills, so a page photo that came out blurred or half-turned is not
 * the only one the backend gets to look at.
 */

import type { CaptureImage, CaptureUploadRequest } from "../protocol";
import type { CapturedImage } from "./api";

/** How many stills one burst takes (#40). */
export const BURST_LENGTH = 3;

/**
 * The pixel edge the camera is asked for. `ideal` is a wish and not a demand: a browser answers
 * with the largest frame the camera has, so this only has to be beyond any camera a laptop carries,
 * and a camera that cannot reach it is not refused for that.
 */
export const MAX_STILL_EDGE_PX = 4096;

/**
 * How long a canvas grab waits for the preview to decode its first frame. `drawImage` of a video
 * that has no frame throws, and pressing Capturar the instant the preview appears is the common
 * case; a preview that never gets one fails the still instead of hanging the button.
 */
export const PREVIEW_READY_TIMEOUT_MS = 2000;

/** The content types `rest.sessions.captures.request` carries an image in. */
const STILL_CONTENT_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;

export type StillContentType = (typeof STILL_CONTENT_TYPES)[number];

/** What the canvas fallback encodes a still in, and how hard it compresses it. */
export const FALLBACK_CONTENT_TYPE: StillContentType = "image/jpeg";
const FALLBACK_QUALITY = 0.92;

/**
 * `HTMLMediaElement.HAVE_CURRENT_DATA`, the readyState from which an element has the frame it is
 * showing and can be drawn. TypeScript's DOM lib does not export the constant itself.
 */
const HAVE_CURRENT_DATA = 2;

/**
 * Why the camera cannot do what the page asked, in the codes the page owns the Spanish for.
 * `permission-denied`, `missing-device` and `in-use` stay apart because the student does something
 * different about each: allow it, plug a camera in, close the application that holds it.
 */
export type CameraProblemCode =
  | "unsupported"
  | "permission-denied"
  | "missing-device"
  | "in-use"
  | "lost"
  | "unavailable";

/** One failure of the camera, which the page turns into a Spanish message for the student. */
export interface CameraProblem {
  readonly code: CameraProblemCode;
  /** The browser's own name and wording, for the log; never shown to the student. */
  readonly detail: string;
}

/**
 * A camera that refused. It carries the problem it was built with, so the page runs a refusal of
 * `start()` and a still that failed mid-session through the same Spanish message.
 */
export class CameraError extends Error implements CameraProblem {
  readonly code: CameraProblemCode;
  readonly detail: string;

  constructor(problem: CameraProblem) {
    super(`${problem.code}: ${problem.detail}`);
    this.name = "CameraError";
    this.code = problem.code;
    this.detail = problem.detail;
  }
}

/** The size `getPhotoSettings()` reports a still in; a camera that says nothing omits both. */
interface PhotoSettingsLike {
  readonly imageWidth?: number;
  readonly imageHeight?: number;
}

/** The pixel range `getPhotoCapabilities()` reports the camera offers a still in. */
interface PhotoCapabilitiesLike {
  readonly imageWidth: { readonly min: number; readonly max: number };
  readonly imageHeight: { readonly min: number; readonly max: number };
}

/**
 * The slice of the Image Capture API this camera uses. TypeScript's DOM lib has no binding for it
 * and not every browser has it at all, so the shape is declared here and the global is looked up by
 * name each time a camera starts.
 */
export interface ImageCaptureLike {
  /**
   * Takes one still at the size the camera chooses, which is its maximum once the track is
   * raised to it.
   */
  takePhoto(): Promise<Blob>;
  getPhotoCapabilities(): Promise<PhotoCapabilitiesLike>;
  getPhotoSettings(): Promise<PhotoSettingsLike>;
}

export interface ImageCaptureConstructor {
  new (track: MediaStreamTrack): ImageCaptureLike;
}

/**
 * A global slot, in the one place this file reads a browser API from. A test environment may keep
 * `window` apart from `globalThis`; a browser never does.
 */
function globalSlot(name: string): unknown {
  const value = (globalThis as Record<string, unknown>)[name];
  if (value !== undefined) return value;
  const scope = (globalThis as unknown as { window?: Record<string, unknown> }).window;
  return scope === undefined ? undefined : scope[name];
}

/** The browser's `ImageCapture`, or null in a browser without the Image Capture API. */
function imageCaptureConstructor(): ImageCaptureConstructor | null {
  const value = globalSlot("ImageCapture");
  return typeof value === "function" ? (value as ImageCaptureConstructor) : null;
}

/** This browser's `ImageCapture` on a track, or null where the Image Capture API does not exist. */
function imageCaptureFor(track: MediaStreamTrack): ImageCaptureLike | null {
  const Constructor = imageCaptureConstructor();
  return Constructor === null ? null : new Constructor(track);
}

/** A pixel size, as the metadata declares it and as the still really is. */
interface Size {
  readonly widthPx: number;
  readonly heightPx: number;
}

/** One still before it is named: its bytes and the size the metadata will declare. */
interface Photo extends Size {
  readonly blob: Blob;
}

/** One still of a burst: the bytes and everything `images[]` declares about them. */
export interface Still extends Size {
  readonly blob: Blob;
  readonly contentType: StillContentType;
  /** The client clock when the shutter went, which is the image's own `client_time_ms`. */
  readonly clientTimeMs: number;
}

/** What asked for a burst: the student's button, or a backend `capture_now` (ADR-0006). */
export type BurstTrigger =
  | { readonly trigger: "button" }
  | { readonly trigger: "command"; readonly commandId: string };

/**
 * One burst ready to upload: the `rest.sessions.captures.request` metadata and the stills its
 * `images[].part` names, which is what `uploadCaptures` takes.
 */
export interface CapturedBurst {
  readonly metadata: CaptureUploadRequest;
  readonly images: readonly CapturedImage[];
}

export interface CameraOptions {
  /** The camera going away by itself, which no call of the page asked for. */
  onLost?(problem: CameraProblem): void;
}

/**
 * The multipart part one still of a burst travels in: `image_0`, `image_1`, ...
 * (protocol/README.md).
 */
export function imagePartName(index: number): string {
  return `image_${index}`;
}

/**
 * What the camera is opened with: the largest frame it has, and no microphone, which is the
 * transcriber's to open, so a burst and a recognition never hold the same device twice.
 */
function previewConstraints(): MediaStreamConstraints {
  return {
    audio: false,
    video: {
      width: { ideal: MAX_STILL_EDGE_PX },
      height: { ideal: MAX_STILL_EDGE_PX },
    },
  };
}

/**
 * What a `getUserMedia` refusal means here: the names the spec gives plus the older ones Chrome and
 * Safari still throw. Anything this table does not know is `unavailable`, which the page explains
 * as a camera that would not work and the log says which name it was.
 */
const REFUSALS: Record<string, CameraProblemCode> = {
  NotAllowedError: "permission-denied",
  PermissionDeniedError: "permission-denied",
  SecurityError: "permission-denied",
  NotFoundError: "missing-device",
  DevicesNotFoundError: "missing-device",
  OverconstrainedError: "missing-device",
  ConstraintNotSatisfiedError: "missing-device",
  NotReadableError: "in-use",
  TrackStartError: "in-use",
};

/**
 * A browser's own name and wording for a failure. Read structurally: a `DOMException` is one, and
 * a browser that reports a refusal some other way still gets logged instead of "[object Object]".
 */
function describe(problem: unknown): string {
  const shaped = problem as { name?: unknown; message?: unknown } | null;
  const name = typeof shaped?.name === "string" ? shaped.name : "";
  const message = typeof shaped?.message === "string" ? shaped.message : String(problem);
  return name === "" ? message : `${name}: ${message}`;
}

function refusalOf(problem: unknown): CameraProblem {
  const name = (problem as { name?: unknown } | null)?.name;
  const code = typeof name === "string" ? REFUSALS[name] : undefined;
  return { code: code ?? "unavailable", detail: describe(problem) };
}

/** Gives every track of a stream back, which is what turns a laptop's camera light off. */
function stopTracks(stream: MediaStream): void {
  for (const track of stream.getTracks()) track.stop();
}

/** Puts the stream on the page's `<video>` and starts it; a page that previews nothing skips it. */
async function showPreview(preview: HTMLVideoElement | null, stream: MediaStream): Promise<void> {
  if (preview === null) return;
  preview.srcObject = stream;
  try {
    await preview.play();
  } catch {
    // A browser that will not autoplay leaves a black preview, which the student can see and the
    // page explains; a still only needs the element to have decoded a frame, which `firstFrame`
    // waits for.
  }
}

/**
 * Resolves once the preview has a frame to draw. The wait is bounded: a preview that never gets one
 * has to fail the still, because a Capturar that never answers looks like a frozen page.
 */
function firstFrame(preview: HTMLVideoElement): Promise<void> {
  if (preview.readyState >= HAVE_CURRENT_DATA) return Promise.resolve();
  return new Promise((resolve) => {
    let timer: ReturnType<typeof setTimeout>;
    const ready = (): void => {
      clearTimeout(timer);
      resolve();
    };
    timer = setTimeout(() => {
      preview.removeEventListener("loadeddata", ready);
      resolve();
    }, PREVIEW_READY_TIMEOUT_MS);
    preview.addEventListener("loadeddata", ready, { once: true });
  });
}

/** The pixel size a track reports, refused when it reports none: the metadata declares one. */
function sizeOf(settings: MediaTrackSettings): Size {
  const widthPx = settings.width ?? 0;
  const heightPx = settings.height ?? 0;
  if (widthPx < 1 || heightPx < 1) {
    throw new CameraError({
      code: "unavailable",
      detail: `the camera reports a ${widthPx}x${heightPx} frame`,
    });
  }
  return { widthPx, heightPx };
}

/**
 * The size a still really has. `getPhotoSettings()` is the camera's own answer and can be larger
 * than the preview stream, which is the point of taking a still through it; the track's settings
 * are the answer when it reports nothing usable.
 */
async function photoSize(capture: ImageCaptureLike, track: MediaStreamTrack): Promise<Size> {
  try {
    const settings = await capture.getPhotoSettings();
    const widthPx = settings.imageWidth ?? 0;
    const heightPx = settings.imageHeight ?? 0;
    if (widthPx >= 1 && heightPx >= 1) return { widthPx, heightPx };
  } catch {
    // A camera that will not say how big its stills are keeps the size its track reports.
  }
  return sizeOf(track.getSettings());
}

/**
 * Raises the track to the largest still the camera offers, which on most cameras is more than the
 * preview stream opened at. It is asked for as an `ideal`, and a camera that refuses keeps the
 * resolution it has instead of failing the whole capture: a smaller page photo is still a page
 * photo, and a camera nobody can use is not.
 */
async function raiseToMaximumStill(
  capture: ImageCaptureLike | null,
  track: MediaStreamTrack,
): Promise<void> {
  if (capture === null) return;
  let maximum: Size;
  try {
    const capabilities = await capture.getPhotoCapabilities();
    maximum = {
      widthPx: capabilities.imageWidth.max,
      heightPx: capabilities.imageHeight.max,
    };
  } catch {
    return;
  }
  const { width = 0, height = 0 } = track.getSettings();
  if (maximum.widthPx <= width || maximum.heightPx <= height) return;
  try {
    await track.applyConstraints({
      width: { ideal: maximum.widthPx },
      height: { ideal: maximum.heightPx },
    });
  } catch {
    // An OverconstrainedError here leaves the preview at the resolution it opened with.
  }
}

/** `canvas.toBlob`, which answers through a callback and with null when it cannot encode. */
function encode(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (blob === null) {
          reject(
            new CameraError({
              code: "unavailable",
              detail: `the browser could not encode the frame as ${FALLBACK_CONTENT_TYPE}`,
            }),
          );
          return;
        }
        resolve(blob);
      },
      FALLBACK_CONTENT_TYPE,
      FALLBACK_QUALITY,
    );
  });
}

/**
 * The content type the still's bytes really are, which is what the metadata declares: the backend
 * stores an image under the type it is told, so a guessed one is a page photo nobody can open.
 */
function stillContentType(blob: Blob): StillContentType {
  const type = blob.type.toLowerCase();
  if ((STILL_CONTENT_TYPES as readonly string[]).includes(type)) return type as StillContentType;
  const produced = type === "" ? "a blob with no type" : `a ${type} blob`;
  throw new CameraError({
    code: "unavailable",
    detail: `the camera produced ${produced}, which the protocol carries no image in`,
  });
}

/** The burst's two halves: the images the uploader posts and the metadata that names them. */
function burstOf(
  trigger: BurstTrigger,
  startedMs: number,
  stills: readonly Still[],
): CapturedBurst {
  const images: CapturedImage[] = [];
  const entries: CaptureImage[] = [];
  stills.forEach((still, index) => {
    const part = imagePartName(index);
    images.push({ part, blob: still.blob });
    entries.push({
      part,
      content_type: still.contentType,
      width_px: still.widthPx,
      height_px: still.heightPx,
      client_time_ms: still.clientTimeMs,
    });
  });
  const metadata: CaptureUploadRequest = {
    // A fresh UUID per burst: it is the idempotency key the backend answers `duplicate` on, so one
    // burst uploaded twice stores once and two bursts never share an id.
    capture_id: crypto.randomUUID(),
    trigger: trigger.trigger,
    client_time_ms: startedMs,
    images: entries,
  };
  if (trigger.trigger === "command") metadata.command_id = trigger.commandId;
  return { metadata, images };
}

/**
 * One laptop camera, from `start()` until `stop()`. Every failure it can have is a `CameraError`,
 * which is what lets the page show one Spanish message per way a camera can refuse.
 */
export class Camera {
  private readonly lost: ((problem: CameraProblem) => void) | undefined;
  private stream: MediaStream | null = null;
  private track: MediaStreamTrack | null = null;
  private capture: ImageCaptureLike | null = null;
  private preview: HTMLVideoElement | null = null;

  constructor(options: CameraOptions = {}) {
    this.lost = options.onLost;
  }

  /** Whether a camera is open and previewing. */
  get running(): boolean {
    return this.track !== null;
  }

  /**
   * Opens the camera and previews it in `preview`, which is also what a canvas grab draws from.
   * Calling it again while it runs does nothing, so the page cannot end up with two streams holding
   * the same device. It rejects with a `CameraError` when there is no camera to open.
   */
  async start(preview?: HTMLVideoElement | null): Promise<void> {
    if (this.track !== null) return;
    const stream = await this.open();
    const track: MediaStreamTrack | undefined = stream.getVideoTracks().at(0);
    if (track === undefined) {
      stopTracks(stream);
      throw new CameraError({
        code: "missing-device",
        detail: "the stream the browser handed out carries no video track",
      });
    }
    this.stream = stream;
    this.track = track;
    this.preview = preview ?? null;
    this.capture = imageCaptureFor(track);
    track.addEventListener("ended", this.onTrackEnded);
    await showPreview(this.preview, stream);
    await raiseToMaximumStill(this.capture, track);
  }

  /** Gives the camera back: the tracks stop, the `<video>` is let go and nothing restarts. */
  stop(): void {
    const stream = this.stream;
    const track = this.track;
    const preview = this.preview;
    this.stream = null;
    this.track = null;
    this.capture = null;
    this.preview = null;
    track?.removeEventListener("ended", this.onTrackEnded);
    if (preview !== null) preview.srcObject = null;
    if (stream !== null) stopTracks(stream);
  }

  /**
   * One still: its bytes, the pixel size the metadata declares and the client clock when the
   * shutter went. The Image Capture API takes it where the browser has it; a browser without it,
   * and a camera whose still capture refuses, get the canvas grab of the preview.
   */
  async takePhoto(): Promise<Still> {
    const track = this.track;
    if (track === null) {
      throw new CameraError({ code: "unavailable", detail: "the camera is not running" });
    }
    const clientTimeMs = Date.now();
    const capture = this.capture;
    const photo = capture === null ? await this.grabFrame(track) : await this.shoot(capture, track);
    return {
      blob: photo.blob,
      contentType: stillContentType(photo.blob),
      widthPx: photo.widthPx,
      heightPx: photo.heightPx,
      clientTimeMs,
    };
  }

  /**
   * One burst of `BURST_LENGTH` stills and the metadata that names them, ready for
   * `uploadCaptures`. A camera that goes away mid-burst keeps the stills it did take -- the
   * protocol asks for at least one image -- and only a burst with nothing in it is a failure.
   */
  async takeBurst(trigger: BurstTrigger): Promise<CapturedBurst> {
    const startedMs = Date.now();
    const stills: Still[] = [];
    while (stills.length < BURST_LENGTH) {
      try {
        stills.push(await this.takePhoto());
      } catch (problem) {
        if (stills.length === 0) throw problem;
        break;
      }
    }
    return burstOf(trigger, startedMs, stills);
  }

  /** Asks the browser for the camera, translating a refusal into the code the page explains. */
  private async open(): Promise<MediaStream> {
    const devices: MediaDevices | undefined = navigator.mediaDevices;
    if (devices === undefined || typeof devices.getUserMedia !== "function") {
      throw new CameraError({
        code: "unsupported",
        detail: "no navigator.mediaDevices.getUserMedia, which only a secure context is given",
      });
    }
    try {
      return await devices.getUserMedia(previewConstraints());
    } catch (problem) {
      throw new CameraError(refusalOf(problem));
    }
  }

  /** The still the Image Capture API takes, at the size the camera says it took it. */
  private async shoot(capture: ImageCaptureLike, track: MediaStreamTrack): Promise<Photo> {
    try {
      const blob = await capture.takePhoto();
      return { blob, ...(await photoSize(capture, track)) };
    } catch {
      // Chrome's still capture fails outright on some laptop cameras; the canvas grab is a smaller
      // photo of the same page, and the student's burst should not be empty over an API quirk.
      return this.grabFrame(track);
    }
  }

  /**
   * The fallback still: the preview drawn onto a canvas at the track's full settings. That is the
   * size the metadata declares, because the canvas is exactly that big and a browser scales the
   * frame it draws into it.
   */
  private async grabFrame(track: MediaStreamTrack): Promise<Photo> {
    const preview = this.preview;
    if (preview === null) {
      throw new CameraError({
        code: "unavailable",
        detail: "no ImageCapture in this browser and no preview element to grab a frame from",
      });
    }
    const { widthPx, heightPx } = sizeOf(track.getSettings());
    await firstFrame(preview);
    const canvas = document.createElement("canvas");
    canvas.width = widthPx;
    canvas.height = heightPx;
    const context = canvas.getContext("2d");
    if (context === null) {
      throw new CameraError({
        code: "unavailable",
        detail: "the browser gives no 2d canvas context to draw the preview on",
      });
    }
    try {
      context.drawImage(preview, 0, 0, widthPx, heightPx);
    } catch (problem) {
      throw new CameraError({
        code: "unavailable",
        detail: `drawing the preview onto the canvas failed: ${describe(problem)}`,
      });
    }
    return { blob: await encode(canvas), widthPx, heightPx };
  }

  /**
   * The camera going away by itself: unplugged, or taken by another application. The page hears it
   * here because nothing of its own asked, and the tracks are let go so the light goes off.
   */
  private readonly onTrackEnded = (): void => {
    const label = this.track?.label ?? "the video track";
    this.stop();
    this.lost?.({
      code: "lost",
      detail: `${label} ended by itself: the camera was unplugged or another application took it`,
    });
  };
}
