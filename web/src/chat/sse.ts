/**
 * A minimal reader of a Server-Sent Events body (`text/event-stream`), for streams that come from
 * a `fetch` POST (the browser's `EventSource` only does GET). Events are separated by a blank
 * line; each carries an `event:` name (default `message`) and one or more `data:` lines, joined
 * with newlines. Comments (`:`) and `id:`/`retry:` fields are ignored.
 */

export interface SseEvent {
  event: string;
  data: string;
}

function parseEvent(raw: string): SseEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split(/\r\n|\r|\n/)) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length === 0 ? null : { event, data: data.join("\n") };
}

/**
 * Reads `body` to its end, calling `onEvent` for every complete event in order. Resolves when the
 * stream ends (a trailing event without its blank line is still delivered); rejects when reading
 * fails (the connection dropped).
 */
export async function readSse(body: ReadableStream<Uint8Array>, onEvent: (event: SseEvent) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flush = (final: boolean) => {
    const parts = buffer.split(/\r\n\r\n|\n\n|\r\r/);
    buffer = final ? "" : (parts.pop() ?? "");
    for (const part of parts) {
      const event = parseEvent(part);
      if (event !== null) onEvent(event);
    }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    flush(false);
  }
  buffer += decoder.decode();
  flush(true);
}
