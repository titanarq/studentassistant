/**
 * The canvas a fallback still is drawn on (#40). jsdom decodes no frame and encodes no image: its
 * `getContext` answers null and its `toBlob` never calls back, both saying so in the test log. A
 * test of the canvas-grab path swaps the two for these fakes, which record what was drawn and
 * answer with a blob whose bytes say which pixel size it stands for.
 */

import { swapProperty } from "./support";

/** One `drawImage` the code under test made: what it drew, where and how big. */
export interface FakeDraw {
  readonly source: unknown;
  readonly dx: number;
  readonly dy: number;
  readonly width: number;
  readonly height: number;
}

/** One `toBlob` the code under test made: the format and quality it asked for. */
export interface FakeEncode {
  readonly type: string | undefined;
  readonly quality: unknown;
}

export interface CanvasFakeOptions {
  /** The content type `toBlob` answers with; `image/jpeg`, which is what the fallback asks for. */
  blobType?: string;
  /** Answer `null` from `toBlob`, which is what a browser that cannot encode does. */
  encodeFails?: boolean;
  /** Answer `null` from `getContext("2d")`, which is what jsdom and a canvas-less browser do. */
  noContext?: boolean;
  /** A `DOMException` name that makes `drawImage` throw ("InvalidStateError"). */
  drawFailure?: string | null;
}

/** Everything the canvas fake does and records; each knob is read when the call happens. */
export interface CanvasFakes {
  /** Every canvas the code under test drew on or asked to encode, in order. */
  readonly canvases: HTMLCanvasElement[];
  readonly draws: FakeDraw[];
  readonly encodes: FakeEncode[];
  blobType: string;
  encodeFails: boolean;
  noContext: boolean;
  drawFailure: string | null;
  restore(): void;
}

/** Swaps `getContext` and `toBlob` on every canvas; the returned `restore()` puts jsdom's back. */
export function installCanvasFakes(options: CanvasFakeOptions = {}): CanvasFakes {
  const fakes: CanvasFakes = {
    canvases: [],
    draws: [],
    encodes: [],
    blobType: options.blobType ?? "image/jpeg",
    encodeFails: options.encodeFails ?? false,
    noContext: options.noContext ?? false,
    drawFailure: options.drawFailure ?? null,
    restore: () => {
      for (const restore of restores.reverse()) restore();
    },
  };
  const context = {
    drawImage(source: unknown, dx: number, dy: number, width: number, height: number): void {
      if (fakes.drawFailure !== null) {
        throw new DOMException("fake drawImage failure", fakes.drawFailure);
      }
      fakes.draws.push({ source, dx, dy, width, height });
    },
  };
  const restores = [
    swapProperty(HTMLCanvasElement.prototype, "getContext", function (this: HTMLCanvasElement) {
      if (!fakes.canvases.includes(this)) fakes.canvases.push(this);
      return fakes.noContext ? null : context;
    }),
    swapProperty(
      HTMLCanvasElement.prototype,
      "toBlob",
      function (
        this: HTMLCanvasElement,
        callback: (blob: Blob | null) => void,
        type?: string,
        quality?: unknown,
      ) {
        if (!fakes.canvases.includes(this)) fakes.canvases.push(this);
        fakes.encodes.push({ type, quality });
        callback(
          fakes.encodeFails
            ? null
            : new Blob([`fake-frame-${this.width}x${this.height}`], { type: fakes.blobType }),
        );
      },
    ),
  ];
  return fakes;
}
