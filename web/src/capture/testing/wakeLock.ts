/**
 * Fakes of the Screen Wake Lock API and of the page's visibility (#256): `navigator.wakeLock`, the
 * `WakeLockSentinel` it grants, and `document.visibilityState` with its `visibilitychange` event.
 * A test says whether the browser grants the lock and when the tab is hidden or shown again.
 */

import { FakeEventTarget, swapProperty } from "./support";

export class FakeWakeLockSentinel extends FakeEventTarget {
  readonly type = "screen";
  released = false;
  onrelease: ((event: Event) => void) | null = null;
  /** How many times the code under test called `release()`. */
  releaseCount = 0;

  async release(): Promise<void> {
    this.releaseCount += 1;
    this.end();
  }

  /** Test control: the browser let the lock go by itself, as it does for a hidden tab. */
  releaseFromBrowser(): void {
    this.end();
  }

  private end(): void {
    if (this.released) return;
    this.released = true;
    this.emit("release");
  }
}

export class FakeWakeLock {
  /** Every sentinel granted, in order. */
  readonly sentinels: FakeWakeLockSentinel[] = [];
  /** How many times `request()` was called, granted or not. */
  requestCount = 0;
  /** A `DOMException` name that makes `request()` reject ("NotAllowedError"). */
  failure: string | null = null;

  async request(type: string): Promise<FakeWakeLockSentinel> {
    this.requestCount += 1;
    if (type !== "screen") throw new TypeError(`fake wake lock of type ${type}`);
    if (this.failure !== null) throw new DOMException("fake wake lock refusal", this.failure);
    const sentinel = new FakeWakeLockSentinel();
    this.sentinels.push(sentinel);
    return sentinel;
  }

  /** The sentinels the code under test holds right now. */
  held(): FakeWakeLockSentinel[] {
    return this.sentinels.filter((sentinel) => !sentinel.released);
  }
}

export interface WakeLockFakes {
  readonly wakeLock: FakeWakeLock;
  restore(): void;
}

/** Puts a fake `navigator.wakeLock` in place; the returned `restore()` takes it off. */
export function installWakeLockFake(): WakeLockFakes {
  const wakeLock = new FakeWakeLock();
  const restore = swapProperty(navigator, "wakeLock", wakeLock);
  return { wakeLock, restore };
}

/**
 * Test control: the tab becomes hidden or visible. `document.visibilityState` and `hidden` report
 * it and `visibilitychange` fires, as a browser does. Returns the restore of the real getters.
 */
export function setVisibility(state: "visible" | "hidden"): () => void {
  const restores = [
    swapProperty(document, "visibilityState", state),
    swapProperty(document, "hidden", state === "hidden"),
  ];
  document.dispatchEvent(new Event("visibilitychange"));
  return () => {
    for (const restore of restores.reverse()) restore();
  };
}
