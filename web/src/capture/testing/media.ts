/**
 * Fakes of the camera and microphone APIs the capture page uses (#40): `navigator.mediaDevices`,
 * `MediaStream`, `MediaStreamTrack` (with the `getSettings()` the still-capture fallback reads the
 * full resolution from) and `ImageCapture`. Nothing here touches a real device: a test says what
 * resolution the camera reports and what a shutter answers with.
 */

import { FakeEventTarget, swapGlobal, swapProperty } from "./support";

/** The camera a fake hands out unless the test asks for another one. */
const DEFAULT_VIDEO_SETTINGS: MediaTrackSettings = {
  deviceId: "fake-camera",
  width: 1280,
  height: 720,
  frameRate: 30,
};

/** The microphone a fake hands out unless the test asks for another one. */
const DEFAULT_AUDIO_SETTINGS: MediaTrackSettings = {
  deviceId: "fake-microphone",
  channelCount: 1,
  sampleRate: 48_000,
  sampleSize: 16,
};

let nextTrackNumber = 1;

export class FakeMediaStreamTrack extends FakeEventTarget {
  readonly id = `fake-track-${nextTrackNumber++}`;
  readonly label: string;
  enabled = true;
  muted = false;
  readyState: "live" | "ended" = "live";
  /** How many times the code under test called `stop()`. */
  stopCount = 0;
  onended: ((event: Event) => void) | null = null;
  /** Every `applyConstraints()` argument, in order. */
  readonly appliedConstraints: MediaTrackConstraints[] = [];
  private settings: MediaTrackSettings;
  private constraints: MediaTrackConstraints;

  constructor(
    readonly kind: "video" | "audio",
    settings: MediaTrackSettings = {},
    constraints: MediaTrackConstraints = {},
  ) {
    super();
    this.settings = { ...settings };
    this.constraints = { ...constraints };
    this.label = `fake ${kind} ${this.id}`;
  }

  getSettings(): MediaTrackSettings {
    return { ...this.settings };
  }

  getConstraints(): MediaTrackConstraints {
    return { ...this.constraints };
  }

  async applyConstraints(constraints: MediaTrackConstraints = {}): Promise<void> {
    this.appliedConstraints.push(constraints);
    this.constraints = { ...constraints };
  }

  /** Test control: make the track report other settings, e.g. a camera that only offers VGA. */
  setSettings(patch: MediaTrackSettings): void {
    this.settings = { ...this.settings, ...patch };
  }

  stop(): void {
    this.stopCount += 1;
    this.end();
  }

  /** Test control: the device went away by itself (unplugged camera, another application took it). */
  endFromDevice(): void {
    this.end();
  }

  private end(): void {
    if (this.readyState === "ended") return;
    this.readyState = "ended";
    this.emit("ended");
  }
}

let nextStreamNumber = 1;

export class FakeMediaStream extends FakeEventTarget {
  readonly id = `fake-stream-${nextStreamNumber++}`;
  /** Test control: what the capture code sees on the `<video>` it previews the stream in. */
  active = true;
  private readonly tracks: FakeMediaStreamTrack[];

  constructor(tracks: FakeMediaStreamTrack[] = []) {
    super();
    this.tracks = [...tracks];
  }

  getTracks(): FakeMediaStreamTrack[] {
    return [...this.tracks];
  }

  getVideoTracks(): FakeMediaStreamTrack[] {
    return this.tracks.filter((track) => track.kind === "video");
  }

  getAudioTracks(): FakeMediaStreamTrack[] {
    return this.tracks.filter((track) => track.kind === "audio");
  }

  getTrackById(id: string): FakeMediaStreamTrack | null {
    return this.tracks.find((track) => track.id === id) ?? null;
  }

  addTrack(track: FakeMediaStreamTrack): void {
    if (!this.tracks.includes(track)) this.tracks.push(track);
  }

  removeTrack(track: FakeMediaStreamTrack): void {
    const index = this.tracks.indexOf(track);
    if (index >= 0) this.tracks.splice(index, 1);
  }
}

/**
 * `ImageCapture.takePhoto()`'s argument. TypeScript's DOM lib has no `ImageCapture`, so the
 * capture code and these fakes spell its shapes out themselves.
 */
export interface FakePhotoSettings {
  imageWidth?: number;
  imageHeight?: number;
}

/** `ImageCapture.getPhotoCapabilities()`'s answer: the pixel range the camera offers. */
export interface FakePhotoCapabilities {
  imageWidth: { min: number; max: number };
  imageHeight: { min: number; max: number };
  fillLightMode: string[];
  redEyeReduction: string;
}

/** Everything the `ImageCapture` fake does and records; `installMediaFakes()` hands it out. */
export class FakePhotoSource {
  /** Blobs `takePhoto()` resolves, first in first out; an empty queue generates one per call. */
  readonly queued: Blob[] = [];
  /** A `DOMException` name that makes `takePhoto()` reject ("UnknownError", "InvalidStateError"). */
  failure: string | null = null;
  /** Every `takePhoto()` call: the settings it got and the pixel size it answered with. */
  readonly taken: Array<{
    settings: FakePhotoSettings | undefined;
    widthPx: number;
    heightPx: number;
  }> = [];
  /** Every `ImageCapture` the code under test constructed, in order. */
  readonly captures: FakeImageCapture[] = [];
}

let activePhotos = new FakePhotoSource();

export class FakeImageCapture {
  constructor(readonly track: FakeMediaStreamTrack) {
    activePhotos.captures.push(this);
  }

  async takePhoto(settings?: FakePhotoSettings): Promise<Blob> {
    const photos = activePhotos;
    if (photos.failure !== null) throw new DOMException("fake ImageCapture failure", photos.failure);
    const { width, height } = this.track.getSettings();
    const widthPx = settings?.imageWidth ?? width ?? 0;
    const heightPx = settings?.imageHeight ?? height ?? 0;
    photos.taken.push({ settings, widthPx, heightPx });
    return photos.queued.shift() ?? fakeStillBlob(widthPx, heightPx);
  }

  async getPhotoCapabilities(): Promise<FakePhotoCapabilities> {
    const { width = 0, height = 0 } = this.track.getSettings();
    return {
      imageWidth: { min: 1, max: width },
      imageHeight: { min: 1, max: height },
      fillLightMode: ["none"],
      redEyeReduction: "never",
    };
  }

  async getPhotoSettings(): Promise<FakePhotoSettings> {
    const { width, height } = this.track.getSettings();
    return { imageWidth: width, imageHeight: height };
  }
}

/** jsdom never decodes a blob, so the bytes say which pixel size the still stands for. */
function fakeStillBlob(widthPx: number, heightPx: number): Blob {
  return new Blob([`fake-still-${widthPx}x${heightPx}`], { type: "image/png" });
}

export class FakeMediaDevices {
  /** Every `getUserMedia()` argument, in order: what the capture code asked the camera for. */
  readonly getUserMediaCalls: MediaStreamConstraints[] = [];
  /** A `DOMException` name that makes `getUserMedia()` reject ("NotAllowedError", "NotFoundError"). */
  failure: string | null = null;
  /** Every stream handed out, in order. */
  readonly streams: FakeMediaStream[] = [];
  private readonly videoTrack: FakeMediaStreamTrack;
  private readonly audioTrack: FakeMediaStreamTrack;

  constructor(videoTrack: FakeMediaStreamTrack, audioTrack: FakeMediaStreamTrack) {
    this.videoTrack = videoTrack;
    this.audioTrack = audioTrack;
  }

  /**
   * Hands out one stream of the tracks the installer built. Every call returns the same track
   * objects, so a test that changes a track's settings or stops it sees what the code sees.
   */
  async getUserMedia(constraints: MediaStreamConstraints = {}): Promise<MediaStream> {
    this.getUserMediaCalls.push(constraints);
    if (this.failure !== null) throw new DOMException("fake getUserMedia failure", this.failure);
    const tracks: FakeMediaStreamTrack[] = [];
    if (constraints.video !== undefined && constraints.video !== false) tracks.push(this.videoTrack);
    if (constraints.audio !== undefined && constraints.audio !== false) tracks.push(this.audioTrack);
    const stream = new FakeMediaStream(tracks);
    this.streams.push(stream);
    return stream as unknown as MediaStream;
  }
}

export interface MediaFakeOptions {
  /** What the fake camera reports from `getSettings()`; merged over a 1280x720 camera. */
  video?: MediaTrackSettings;
  /** What the fake microphone reports from `getSettings()`; merged over 48 kHz mono. */
  audio?: MediaTrackSettings;
  /** Install `ImageCapture` too, as every browser the capture page supports does; default true.
   * Pass false for the canvas-grab path of a browser without it. */
  imageCapture?: boolean;
}

export interface MediaFakes {
  readonly devices: FakeMediaDevices;
  readonly videoTrack: FakeMediaStreamTrack;
  readonly audioTrack: FakeMediaStreamTrack;
  readonly photos: FakePhotoSource;
  restore(): void;
}

/** Puts the media fakes on `navigator` and `globalThis`; the returned `restore()` takes them off. */
export function installMediaFakes(options: MediaFakeOptions = {}): MediaFakes {
  const videoTrack = new FakeMediaStreamTrack("video", {
    ...DEFAULT_VIDEO_SETTINGS,
    ...options.video,
  });
  const audioTrack = new FakeMediaStreamTrack("audio", {
    ...DEFAULT_AUDIO_SETTINGS,
    ...options.audio,
  });
  const devices = new FakeMediaDevices(videoTrack, audioTrack);
  activePhotos = new FakePhotoSource();
  const restores = [swapProperty(navigator, "mediaDevices", devices)];
  if (options.imageCapture !== false) restores.push(swapGlobal("ImageCapture", FakeImageCapture));
  return {
    devices,
    videoTrack,
    audioTrack,
    photos: activePhotos,
    restore: () => {
      for (const restore of restores.reverse()) restore();
    },
  };
}
