/**
 * The screen wake lock of a running capture session (#256). A twenty-minute session on a laptop is
 * longer than most screen timeouts, and a screen that dims and locks takes the camera preview, the
 * recognizer and the student's attention with it. So while a session runs the page holds a Screen
 * Wake Lock, and because a browser releases it by itself whenever the tab is hidden, it asks again
 * each time the page becomes visible.
 *
 * The lock is a convenience and never a condition of the session: a browser without the API, a
 * request the browser refuses (battery saver, no user activation) or a release that fails are all
 * silent. Nothing here throws and nothing here is shown to the student.
 */

/** The slice of `WakeLockSentinel` this file uses. */
export interface WakeLockSentinelLike {
  readonly released: boolean;
  release(): Promise<void>;
  addEventListener(type: "release", listener: () => void): void;
  removeEventListener(type: "release", listener: () => void): void;
}

/** The slice of `navigator.wakeLock` this file uses. */
export interface WakeLockLike {
  request(type: "screen"): Promise<WakeLockSentinelLike>;
}

/** The slice of `document` the re-acquisition listens to. */
export interface VisibilitySource {
  readonly visibilityState: DocumentVisibilityState;
  addEventListener(type: "visibilitychange", listener: () => void): void;
  removeEventListener(type: "visibilitychange", listener: () => void): void;
}

export interface ScreenWakeLockOptions {
  /** The `navigator.wakeLock` to ask; read off `navigator` when left out. Null means no API. */
  wakeLock?: WakeLockLike | null;
  /** The document whose visibility re-acquires the lock; `document` when left out. */
  visibility?: VisibilitySource;
}

/** This browser's `navigator.wakeLock`, or null where the Screen Wake Lock API does not exist. */
function browserWakeLock(): WakeLockLike | null {
  if (typeof navigator === "undefined") return null;
  const candidate = (navigator as unknown as { wakeLock?: unknown }).wakeLock;
  if (typeof candidate !== "object" || candidate === null) return null;
  return typeof (candidate as { request?: unknown }).request === "function"
    ? (candidate as WakeLockLike)
    : null;
}

/**
 * One session's screen wake lock, from `start()` until `stop()`. Both are idempotent, so the screen
 * can call `stop()` on Terminar and again on unmount without caring which came first.
 */
export class ScreenWakeLock {
  private readonly wakeLock: WakeLockLike | null;
  private readonly visibility: VisibilitySource | null;
  private active = false;
  private sentinel: WakeLockSentinelLike | null = null;
  private requesting = false;

  constructor(options: ScreenWakeLockOptions = {}) {
    this.wakeLock = options.wakeLock === undefined ? browserWakeLock() : options.wakeLock;
    this.visibility =
      options.visibility ?? (typeof document === "undefined" ? null : document);
  }

  /** Whether a lock is held right now: the browser granted it and has not released it. */
  get held(): boolean {
    return this.sentinel !== null && !this.sentinel.released;
  }

  /** Holds the screen awake from now on, and again every time the page becomes visible. */
  start(): void {
    if (this.active) return;
    this.active = true;
    this.visibility?.addEventListener("visibilitychange", this.onVisibilityChange);
    void this.acquire();
  }

  /** Lets the screen sleep again and stops listening; a lock still being requested is let go on arrival. */
  stop(): void {
    if (!this.active) return;
    this.active = false;
    this.visibility?.removeEventListener("visibilitychange", this.onVisibilityChange);
    const sentinel = this.sentinel;
    this.sentinel = null;
    if (sentinel !== null) this.releaseQuietly(sentinel);
  }

  private async acquire(): Promise<void> {
    if (this.wakeLock === null || this.requesting || this.held) return;
    if (this.visibility !== null && this.visibility.visibilityState !== "visible") return;
    this.requesting = true;
    let sentinel: WakeLockSentinelLike;
    try {
      sentinel = await this.wakeLock.request("screen");
    } catch {
      // Refused (battery saver, a hidden page, a policy): the session goes on with a screen that
      // may sleep, which is what it had before this file existed.
      return;
    } finally {
      this.requesting = false;
    }
    if (!this.active) {
      // The session ended while the browser was deciding: the lock arrived for nobody.
      this.releaseQuietly(sentinel);
      return;
    }
    sentinel.addEventListener("release", this.onReleased);
    this.sentinel = sentinel;
  }

  private releaseQuietly(sentinel: WakeLockSentinelLike): void {
    sentinel.removeEventListener("release", this.onReleased);
    if (sentinel.released) return;
    try {
      void sentinel.release().catch(() => {
        // Already released by the browser: nothing to give back.
      });
    } catch {
      // A sentinel whose release throws synchronously is one there is nothing more to do about.
    }
  }

  /** The browser let the lock go by itself -- a hidden tab always does. */
  private readonly onReleased = (): void => {
    const sentinel = this.sentinel;
    if (sentinel === null) return;
    sentinel.removeEventListener("release", this.onReleased);
    this.sentinel = null;
  };

  private readonly onVisibilityChange = (): void => {
    if (!this.active || this.visibility?.visibilityState !== "visible") return;
    void this.acquire();
  };
}
