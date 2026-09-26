/**
 * The live connection of the workspace chat panel (#317) to `GET .../workspace/stream` (#315),
 * read with `fetch` and `readSse` like the chat's own streams. The backend neither persists nor
 * replays these events, so a connection that drops or fails is opened again after a backoff
 * (`RETRY_DELAYS_MS`, the last delay repeating, reset by every successful open), and `onOpen`
 * tells the caller each time it is open again so it can re-read the history and the notes.
 */

import { readSse } from "../../chat/sse";
import { readWorkspaceEvent, type WorkspaceEvent, workspaceStreamPath } from "./api";

/** Waits before each new attempt: 1 s, 2 s, 5 s, 10 s, then every 30 s. */
export const RETRY_DELAYS_MS: readonly number[] = [1000, 2000, 5000, 10000, 30000];

export interface StreamCallbacks {
  onEvent: (event: WorkspaceEvent) => void;
  /** The stream is open; `reconnected` is false the first time only. */
  onOpen: (reconnected: boolean) => void;
  /** The stream dropped, or an attempt to open it failed; another attempt follows. */
  onDown: () => void;
  retryDelays?: readonly number[];
}

/** Opens the topic's workspace stream and keeps it open until the returned `close()`. */
export function connectWorkspaceStream(subjectId: string, topicId: string, callbacks: StreamCallbacks): () => void {
  const path = workspaceStreamPath(subjectId, topicId);
  const delays = callbacks.retryDelays !== undefined && callbacks.retryDelays.length > 0 ? callbacks.retryDelays : RETRY_DELAYS_MS;
  let closed = false;
  let opened = false;
  let failures = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let controller: AbortController | null = null;

  const retry = () => {
    if (closed) return;
    callbacks.onDown();
    const delay = delays[Math.min(failures, delays.length - 1)];
    failures += 1;
    timer = setTimeout(() => {
      timer = null;
      void connect();
    }, delay);
  };

  const connect = async () => {
    if (closed) return;
    controller = new AbortController();
    let response: Response;
    try {
      response = await fetch(path, { headers: { Accept: "text/event-stream" }, signal: controller.signal });
    } catch {
      retry();
      return;
    }
    if (closed) return;
    if (!response.ok || response.body === null) {
      retry();
      return;
    }
    failures = 0;
    callbacks.onOpen(opened);
    opened = true;
    try {
      await readSse(response.body, ({ event, data }) => {
        if (closed) return;
        const decoded = readWorkspaceEvent(event, data);
        if (decoded !== null) callbacks.onEvent(decoded);
      });
    } catch {
      // The connection dropped: open it again below.
    }
    retry();
  };

  void connect();
  return () => {
    closed = true;
    if (timer !== null) clearTimeout(timer);
    controller?.abort();
  };
}
