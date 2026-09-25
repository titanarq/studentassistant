/**
 * The camera against the fake media devices, `ImageCapture` and canvas (#40). Both still paths are
 * covered -- the Image Capture API's shutter and the canvas grab a browser without it falls back to
 * -- and every burst's metadata goes through the bindings' own `parseMessage`, so what the page
 * would upload is contract-valid instead of merely matching what this test invented.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CaptureUploadRequest } from "../protocol";
import { parseMessage } from "../protocol";
import {
  BURST_LENGTH,
  Camera,
  CameraError,
  type CapturedBurst,
  type CameraProblem,
  type CameraProblemCode,
  FALLBACK_CONTENT_TYPE,
  MAX_STILL_EDGE_PX,
  PREVIEW_READY_TIMEOUT_MS,
} from "./camera";
import {
  type CanvasFakes,
  FakeImageCapture,
  FakeMediaStream,
  type FakePreview,
  fakePreview,
  installCanvasFakes,
  installMediaFakes,
  type MediaFakes,
  swapGlobal,
  swapProperty,
} from "./testing";

/** The client clock a burst starts at, of the same era as `protocol/examples/`. */
const START_MS = 1790251260000;

/** A camera whose stills are 4032x3024, more than the 1280x720 preview stream it opens. */
const SENSOR = { width: 4032, height: 3024 } as const;

const restores: Array<() => void> = [];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
  vi.setSystemTime(START_MS);
});

afterEach(() => {
  vi.useRealTimers();
  while (restores.length > 0) restores.pop()?.();
});

interface Harness {
  readonly camera: Camera;
  readonly media: MediaFakes;
  readonly canvas: CanvasFakes;
  readonly preview: FakePreview;
  /** Every camera the page was told went away by itself. */
  readonly lost: CameraProblem[];
}

/**
 * The camera on the fakes. `imageCapture: false` is the browser without the Image Capture API,
 * which is the one that takes its stills by drawing the preview onto a canvas.
 */
function harness(options: { imageCapture?: boolean } = {}): Harness {
  const media = installMediaFakes(options);
  restores.push(media.restore);
  const canvas = installCanvasFakes();
  restores.push(canvas.restore);
  const preview = fakePreview();
  const lost: CameraProblem[] = [];
  const camera = new Camera({ onLost: (problem) => lost.push(problem) });
  return { camera, media, canvas, preview, lost };
}

/** The harness's camera, already previewing. */
async function started(options: { imageCapture?: boolean } = {}): Promise<Harness> {
  const built = harness(options);
  await built.camera.start(built.preview.element);
  return built;
}

/** The `CameraError` a call refused with, which is the only failure this module has. */
async function refusal(promise: Promise<unknown>): Promise<CameraError> {
  const problem = await promise.then(
    () => null,
    (caught: unknown) => caught,
  );
  if (!(problem instanceof CameraError)) {
    throw new Error(`the camera answered with ${String(problem)} instead of refusing`);
  }
  return problem;
}

/** The burst's metadata as it goes on the wire, decoded by the bindings it has to satisfy. */
function decodedMetadata(burst: CapturedBurst): CaptureUploadRequest {
  return parseMessage(
    "rest.sessions.captures.request",
    JSON.parse(JSON.stringify(burst.metadata)) as unknown,
  );
}

/** An `ImageCapture` of a camera whose stills are bigger than its preview stream. */
function installSensorSize(size: { width: number; height: number }): void {
  class SensorImageCapture extends FakeImageCapture {
    override async getPhotoCapabilities() {
      return {
        imageWidth: { min: 1, max: size.width },
        imageHeight: { min: 1, max: size.height },
        fillLightMode: ["none"],
        redEyeReduction: "never",
      };
    }
  }
  restores.push(swapGlobal("ImageCapture", SensorImageCapture));
}

/** An `ImageCapture` whose shutter fails from its `failFrom`-th call on: a camera going away. */
function installFlakyShutter(failFrom: number): void {
  let calls = 0;
  class FlakyImageCapture extends FakeImageCapture {
    override async takePhoto(): Promise<Blob> {
      calls += 1;
      if (calls >= failFrom) throw new DOMException("fake ImageCapture failure", "UnknownError");
      return super.takePhoto();
    }
  }
  restores.push(swapGlobal("ImageCapture", FlakyImageCapture));
}

describe("starting the preview", () => {
  it("asks for the largest frame the camera has and no microphone", async () => {
    const built = await started();
    expect(built.media.devices.getUserMediaCalls).toEqual([
      {
        // The microphone belongs to the transcriber, which opens its own stream.
        audio: false,
        video: {
          width: { ideal: MAX_STILL_EDGE_PX },
          height: { ideal: MAX_STILL_EDGE_PX },
        },
      },
    ]);
    expect(built.camera.running).toBe(true);
  });

  it("previews the stream it was given and plays it", async () => {
    const built = await started();
    expect(built.preview.sources).toEqual([built.media.devices.streams[0]]);
    expect(built.preview.playCount).toBe(1);
  });

  it("takes its stills through ImageCapture on the video track", async () => {
    const built = await started();
    expect(built.media.photos.captures).toHaveLength(1);
    expect(built.media.photos.captures[0].track).toBe(built.media.videoTrack);
  });

  it("raises the track to the largest still the camera offers", async () => {
    const built = harness();
    installSensorSize(SENSOR);
    await built.camera.start(built.preview.element);
    expect(built.media.videoTrack.appliedConstraints).toEqual([
      { width: { ideal: SENSOR.width }, height: { ideal: SENSOR.height } },
    ]);
  });

  it("keeps the resolution it opened at when the camera offers no bigger still", async () => {
    // The fake's capabilities are the track's own settings, which the stream already opened at.
    const built = await started();
    expect(built.media.videoTrack.appliedConstraints).toEqual([]);
  });

  it("keeps the resolution it opened at when the camera refuses the raise", async () => {
    const built = harness();
    installSensorSize(SENSOR);
    built.media.videoTrack.applyConstraints = async () => {
      throw new DOMException("fake constraint failure", "OverconstrainedError");
    };
    await expect(built.camera.start(built.preview.element)).resolves.toBeUndefined();
    expect(built.camera.running).toBe(true);
    expect(built.media.videoTrack.getSettings().width).toBe(1280);
  });

  it("opens no second stream when it is already previewing", async () => {
    const built = await started();
    await built.camera.start(built.preview.element);
    expect(built.media.devices.getUserMediaCalls).toHaveLength(1);
    expect(built.media.devices.streams).toHaveLength(1);
  });

  it("takes stills with no preview element when the browser has ImageCapture", async () => {
    const built = harness();
    await built.camera.start();
    const still = await built.camera.takePhoto();
    expect(still.widthPx).toBe(1280);
    expect(built.preview.playCount).toBe(0);
  });

  const REFUSED: Array<[string, CameraProblemCode]> = [
    ["NotAllowedError", "permission-denied"],
    ["PermissionDeniedError", "permission-denied"],
    ["SecurityError", "permission-denied"],
    ["NotFoundError", "missing-device"],
    ["DevicesNotFoundError", "missing-device"],
    ["OverconstrainedError", "missing-device"],
    ["NotReadableError", "in-use"],
    ["TrackStartError", "in-use"],
    ["UnknownError", "unavailable"],
  ];

  it.each(REFUSED)("maps a %s refusal to the %s code", async (name, code) => {
    const built = harness();
    built.media.devices.failure = name;
    const problem = await refusal(built.camera.start(built.preview.element));
    expect(problem.code).toBe(code);
    // The browser's own name goes to the log; the student sees the Spanish of the code.
    expect(problem.detail).toContain(name);
    expect(built.camera.running).toBe(false);
  });

  it("refuses a browser that offers no camera API at all", async () => {
    // No media fakes: jsdom's navigator has no `mediaDevices`, which is what a page served outside
    // a secure context gets too.
    const problem = await refusal(new Camera().start(fakePreview().element));
    expect(problem.code).toBe("unsupported");
    expect(problem.detail).toContain("getUserMedia");
  });

  it("refuses a stream that carries no video track", async () => {
    const built = harness();
    restores.push(
      swapProperty(navigator, "mediaDevices", {
        getUserMedia: async () => new FakeMediaStream([]) as unknown as MediaStream,
      }),
    );
    const problem = await refusal(built.camera.start(built.preview.element));
    expect(problem.code).toBe("missing-device");
    expect(problem.detail).toContain("no video track");
  });
});

describe("a still through the Image Capture API", () => {
  it("answers with the shutter's blob, its content type and its size", async () => {
    const built = await started();
    built.media.videoTrack.setSettings({ width: SENSOR.width, height: SENSOR.height });
    const still = await built.camera.takePhoto();
    expect(await still.blob.text()).toBe(`fake-still-${SENSOR.width}x${SENSOR.height}`);
    expect(still.contentType).toBe("image/png");
    expect(still.widthPx).toBe(SENSOR.width);
    expect(still.heightPx).toBe(SENSOR.height);
    expect(still.clientTimeMs).toBe(START_MS);
  });

  it("lets the camera choose the size, which is its maximum once the track is raised", async () => {
    const built = await started();
    await built.camera.takePhoto();
    expect(built.media.photos.taken).toEqual([
      { settings: undefined, widthPx: 1280, heightPx: 720 },
    ]);
  });

  it("declares the content type each still's bytes really are", async () => {
    const built = await started();
    built.media.photos.queued.push(new Blob(["still"], { type: "image/jpeg" }));
    expect((await built.camera.takePhoto()).contentType).toBe("image/jpeg");
    built.media.photos.queued.push(new Blob(["still"], { type: "image/webp" }));
    expect((await built.camera.takePhoto()).contentType).toBe("image/webp");
  });

  it("carries the client clock of the moment each shutter went", async () => {
    const built = await started();
    const first = await built.camera.takePhoto();
    vi.setSystemTime(START_MS + 1500);
    const second = await built.camera.takePhoto();
    expect([first.clientTimeMs, second.clientTimeMs]).toEqual([START_MS, START_MS + 1500]);
  });

  it("refuses a still in a format the protocol carries no image in", async () => {
    const built = await started();
    built.media.photos.queued.push(new Blob(["still"], { type: "image/bmp" }));
    const problem = await refusal(built.camera.takePhoto());
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("image/bmp");
  });

  it("refuses a still whose blob says nothing about its format", async () => {
    const built = await started();
    built.media.photos.queued.push(new Blob(["still"]));
    const problem = await refusal(built.camera.takePhoto());
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("a blob with no type");
  });

  it("refuses a still taken before the camera was started", async () => {
    const problem = await refusal(new Camera().takePhoto());
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("not running");
  });
});

describe("a still through the canvas grab", () => {
  it("draws the preview at the track's full settings and encodes it as a JPEG", async () => {
    const built = await started({ imageCapture: false });
    built.media.videoTrack.setSettings({ width: 1920, height: 1080 });
    const still = await built.camera.takePhoto();
    expect(built.canvas.canvases).toHaveLength(1);
    expect([built.canvas.canvases[0].width, built.canvas.canvases[0].height]).toEqual([1920, 1080]);
    expect(built.canvas.draws).toEqual([
      { source: built.preview.element, dx: 0, dy: 0, width: 1920, height: 1080 },
    ]);
    expect(built.canvas.encodes).toEqual([
      { type: FALLBACK_CONTENT_TYPE, quality: expect.any(Number) },
    ]);
    expect(still.contentType).toBe(FALLBACK_CONTENT_TYPE);
    expect([still.widthPx, still.heightPx]).toEqual([1920, 1080]);
    expect(still.clientTimeMs).toBe(START_MS);
    expect(await still.blob.text()).toBe("fake-frame-1920x1080");
  });

  it("waits for the preview's first frame before drawing it", async () => {
    const built = await started({ imageCapture: false });
    built.preview.makeNotReady();
    const pending = built.camera.takePhoto();
    expect(built.canvas.canvases).toEqual([]);
    built.preview.emitLoadedData();
    const still = await pending;
    expect(built.canvas.draws).toHaveLength(1);
    expect(still.widthPx).toBe(1280);
  });

  it("fails the still when the preview never gets a frame to draw", async () => {
    const built = await started({ imageCapture: false });
    built.preview.makeNotReady();
    // What a real browser does then: `drawImage` of a video that decoded nothing throws.
    built.canvas.drawFailure = "InvalidStateError";
    const pending = built.camera.takePhoto();
    // The refusal is caught before the clock moves, so the rejection is never an unhandled one.
    const caught = refusal(pending);
    await vi.advanceTimersByTimeAsync(PREVIEW_READY_TIMEOUT_MS);
    const problem = await caught;
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("drawing the preview");
  });

  it("refuses when the browser gives no 2d canvas context", async () => {
    const built = await started({ imageCapture: false });
    built.canvas.noContext = true;
    const problem = await refusal(built.camera.takePhoto());
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("2d canvas context");
  });

  it("refuses when the browser cannot encode the frame", async () => {
    const built = await started({ imageCapture: false });
    built.canvas.encodeFails = true;
    const problem = await refusal(built.camera.takePhoto());
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("could not encode");
  });

  it("refuses when the page previews the camera in no element", async () => {
    const built = harness({ imageCapture: false });
    await built.camera.start();
    const problem = await refusal(built.camera.takePhoto());
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("preview element");
  });

  it("refuses when the camera reports no resolution to draw at", async () => {
    const built = await started({ imageCapture: false });
    built.media.videoTrack.setSettings({ width: 0, height: 0 });
    const problem = await refusal(built.camera.takePhoto());
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("0x0");
  });

  it("grabs the canvas when the shutter refuses", async () => {
    const built = await started();
    built.media.photos.failure = "UnknownError";
    const still = await built.camera.takePhoto();
    expect(built.media.photos.taken).toEqual([]);
    expect(built.canvas.draws).toHaveLength(1);
    expect(still.contentType).toBe(FALLBACK_CONTENT_TYPE);
    expect(still.widthPx).toBe(1280);
  });

  it("refuses when both the shutter and the canvas grab fail", async () => {
    const built = await started();
    built.media.photos.failure = "UnknownError";
    built.canvas.noContext = true;
    const problem = await refusal(built.camera.takePhoto());
    expect(problem.code).toBe("unavailable");
    expect(problem.detail).toContain("2d canvas context");
  });
});

describe("a burst", () => {
  it("takes three stills and names each one in metadata the protocol accepts", async () => {
    const built = await started();
    built.media.videoTrack.setSettings({ width: SENSOR.width, height: SENSOR.height });
    for (const name of ["primera", "segunda", "tercera"]) {
      built.media.photos.queued.push(new Blob([name], { type: "image/jpeg" }));
    }
    const burst = await built.camera.takeBurst({ trigger: "button" });
    expect(burst.metadata.images).toHaveLength(BURST_LENGTH);
    expect(burst.metadata.images).toEqual(
      [0, 1, 2].map((index) => ({
        part: `image_${index}`,
        content_type: "image/jpeg",
        width_px: SENSOR.width,
        height_px: SENSOR.height,
        client_time_ms: START_MS,
      })),
    );
    expect(burst.metadata.trigger).toBe("button");
    expect(burst.metadata.command_id).toBeUndefined();
    expect(burst.metadata.client_time_ms).toBe(START_MS);
    expect(burst.metadata.capture_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    expect(decodedMetadata(burst)).toEqual(burst.metadata);
    // The parts the uploader posts are the ones the metadata declares, in the same order, and
    // carry the stills the shutter gave.
    expect(burst.images.map((image) => image.part)).toEqual(["image_0", "image_1", "image_2"]);
    expect(await Promise.all(burst.images.map((image) => image.blob.text()))).toEqual([
      "primera",
      "segunda",
      "tercera",
    ]);
  });

  it("carries the command_id of the command that asked for it", async () => {
    const built = await started();
    const burst = await built.camera.takeBurst({ trigger: "command", commandId: "cmd-17" });
    expect(burst.metadata.trigger).toBe("command");
    expect(burst.metadata.command_id).toBe("cmd-17");
    expect(decodedMetadata(burst)).toEqual(burst.metadata);
  });

  it("gives every burst its own capture_id, which is what makes an upload idempotent", async () => {
    const built = await started();
    const first = await built.camera.takeBurst({ trigger: "button" });
    const second = await built.camera.takeBurst({ trigger: "button" });
    expect(first.metadata.capture_id).not.toBe(second.metadata.capture_id);
  });

  it("keeps the stills it took before the camera went away", async () => {
    const built = harness({ imageCapture: false });
    installFlakyShutter(2);
    // Nothing can save the second still: the shutter fails and so does the canvas fallback.
    built.canvas.noContext = true;
    await built.camera.start(built.preview.element);
    const burst = await built.camera.takeBurst({ trigger: "button" });
    expect(burst.metadata.images).toHaveLength(1);
    expect(burst.images.map((image) => image.part)).toEqual(["image_0"]);
    expect(decodedMetadata(burst)).toEqual(burst.metadata);
  });

  it("refuses a burst whose first still failed", async () => {
    const built = harness({ imageCapture: false });
    installFlakyShutter(1);
    built.canvas.noContext = true;
    await built.camera.start(built.preview.element);
    const problem = await refusal(built.camera.takeBurst({ trigger: "button" }));
    expect(problem.code).toBe("unavailable");
  });
});

describe("stopping", () => {
  it("gives the camera back and lets the preview go", async () => {
    const built = await started();
    built.camera.stop();
    expect(built.camera.running).toBe(false);
    expect(built.media.videoTrack.stopCount).toBe(1);
    expect(built.media.videoTrack.readyState).toBe("ended");
    expect(built.preview.sources.at(-1)).toBeNull();
  });

  it("stops listening for a track that ends by itself", async () => {
    const built = await started();
    expect(built.media.videoTrack.listenerCount("ended")).toBe(1);
    built.camera.stop();
    expect(built.media.videoTrack.listenerCount("ended")).toBe(0);
  });

  it("refuses a still after it", async () => {
    const built = await started();
    built.camera.stop();
    const problem = await refusal(built.camera.takePhoto());
    expect(problem.code).toBe("unavailable");
  });

  it("reports a camera that went away by itself and lets its tracks go", async () => {
    const built = await started();
    built.media.videoTrack.endFromDevice();
    expect(built.lost).toHaveLength(1);
    expect(built.lost[0].code).toBe("lost");
    expect(built.camera.running).toBe(false);
    expect(built.preview.sources.at(-1)).toBeNull();
    expect(built.media.videoTrack.stopCount).toBe(1);
  });
});
