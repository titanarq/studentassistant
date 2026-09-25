import { act } from "@testing-library/react";
import type { LiveSource, LiveSourceFactory } from "./live";

/** A fake `EventSource` the test drives: `emit` delivers one named event, `drop`/`open` the connection. */
export class FakeLiveSource implements LiveSource {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  private listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  constructor(readonly url: string) {}

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  emit(name: string, data: unknown): void {
    const message = new MessageEvent<string>(name, { data: typeof data === "string" ? data : JSON.stringify(data) });
    act(() => {
      for (const listener of this.listeners.get(name) ?? []) listener(message);
    });
  }

  drop(): void {
    act(() => this.onerror?.(new Event("error")));
  }

  open(): void {
    act(() => this.onopen?.(new Event("open")));
  }
}

/** A factory that records every source it opens. */
export function fakeSources() {
  const sources: FakeLiveSource[] = [];
  const factory: LiveSourceFactory = (url) => {
    const source = new FakeLiveSource(url);
    sources.push(source);
    return source;
  };
  return { sources, factory, get last() { return sources[sources.length - 1]; } };
}

export const SESSION = { session_id: "s-1", subject_id: "fisica", topic_id: "cinematica", started_at_ms: 1_700_000_000_000 };

export function snapshot(overrides: Record<string, unknown> = {}) {
  return { session: SESSION, segments: [], captures: [], outline: [], open_pending: 0, ...overrides };
}

export function capture(overrides: Record<string, unknown> = {}) {
  return {
    capture_id: "cap-1",
    t: 65_000,
    page_path: "subjects/fisica/topics/cinematica/sources/notes/page-001.page.jpg",
    source_path: "subjects/fisica/topics/cinematica/sources/notes/page-001.jpg",
    source_context: "notes",
    status: "pending",
    text: null,
    page_number: null,
    message: null,
    ...overrides,
  };
}
