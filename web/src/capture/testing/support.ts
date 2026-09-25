/**
 * Shared plumbing of the capture fakes (#40).
 *
 * jsdom implements none of the media, speech-recognition or Web Audio APIs the capture page
 * uses, and the `WebSocket` it does implement opens real connections, so a capture test swaps
 * those globals for the fakes in this directory. `swapGlobal` and `swapProperty` do the swap and
 * hand back the restore that puts the previous value in place, which is what keeps one test's
 * fakes out of the next one's way.
 */

/** A dispatched event. The fakes build real `Event` objects, so `type` is all of them share. */
export interface FakeEvent {
  readonly type: string;
}

// `any` on purpose: a handler written against the DOM's own event type has to stay assignable to
// whatever a fake accepts, and only `any` is assignable in both directions.
export type FakeEventListener = (event: any) => void;

interface ListenerEntry {
  readonly listener: FakeEventListener;
  readonly once: boolean;
}

/** `addEventListener` plus the `on<type>` property, dispatching to both like the DOM does. */
export class FakeEventTarget {
  private readonly listenersByType = new Map<string, ListenerEntry[]>();

  addEventListener(
    type: string,
    listener: FakeEventListener,
    options?: boolean | AddEventListenerOptions,
  ): void {
    const once = typeof options === "object" && options !== null && options.once === true;
    const entries = this.listenersByType.get(type) ?? [];
    if (!entries.some((entry) => entry.listener === listener)) entries.push({ listener, once });
    this.listenersByType.set(type, entries);
  }

  removeEventListener(type: string, listener: FakeEventListener): void {
    const entries = this.listenersByType.get(type);
    if (entries === undefined) return;
    this.listenersByType.set(
      type,
      entries.filter((entry) => entry.listener !== listener),
    );
  }

  /** How many listeners the code under test attached for `type`. */
  listenerCount(type: string): number {
    return (this.listenersByType.get(type) ?? []).length;
  }

  /** Runs the `on<type>` handler the code under test assigned, then every listener of `type`. */
  protected dispatch(event: FakeEvent): void {
    const handler = (this as unknown as Record<string, unknown>)[`on${event.type}`];
    if (typeof handler === "function") (handler as FakeEventListener).call(this, event);
    for (const entry of [...(this.listenersByType.get(event.type) ?? [])]) {
      if (entry.once) this.removeEventListener(event.type, entry.listener);
      entry.listener.call(this, event);
    }
  }

  /** Dispatches a plain `Event` of `type` carrying `init`'s extra fields. */
  protected emit(type: string, init?: Record<string, unknown>): void {
    this.dispatch(Object.assign(new Event(type), init) as unknown as FakeEvent);
  }
}

/** `globalThis` and, when vitest's jsdom keeps a separate object, `window` too. */
function globalOwners(): object[] {
  const other = (globalThis as { window?: unknown }).window;
  if (typeof other === "object" && other !== null && other !== globalThis) return [globalThis, other];
  return [globalThis];
}

/** Puts `value` in `owner[key]` and returns the restore of whatever was there before. */
export function swapProperty(owner: object, key: string, value: unknown): () => void {
  const previous = Object.getOwnPropertyDescriptor(owner, key);
  Object.defineProperty(owner, key, {
    value,
    configurable: true,
    writable: true,
    enumerable: true,
  });
  return () => {
    if (previous === undefined) delete (owner as Record<string, unknown>)[key];
    else Object.defineProperty(owner, key, previous);
  };
}

/** `swapProperty` on every global object vitest's jsdom mirrors. */
export function swapGlobal(key: string, value: unknown): () => void {
  const restores = globalOwners().map((owner) => swapProperty(owner, key, value));
  return () => {
    for (const restore of restores.reverse()) restore();
  };
}

/** What a global slot holds right now, for the code and the tests that look a browser API up. */
export function readGlobal(key: string): unknown {
  const value = (globalThis as Record<string, unknown>)[key];
  if (value !== undefined) return value;
  const other = (globalThis as unknown as { window?: Record<string, unknown> }).window;
  return other === undefined ? undefined : other[key];
}
