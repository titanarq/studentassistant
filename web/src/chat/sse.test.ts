import { expect, it } from "vitest";
import { readSse, type SseEvent } from "./sse";

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(chunks: string[]): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  await readSse(streamOf(chunks), (event) => events.push(event));
  return events;
}

it("reads events split anywhere across chunks", async () => {
  const text = 'event: reply.delta\ndata: {"text":"Hola, ñandú"}\n\nevent: result\ndata: {"ok":true}\n\n';
  const chunks = [...text].map((c) => c); // one character per chunk
  expect(await collect(chunks)).toEqual([
    { event: "reply.delta", data: '{"text":"Hola, ñandú"}' },
    { event: "result", data: '{"ok":true}' },
  ]);
});

it("splits a multi-byte character across chunks without mangling it", async () => {
  const bytes = new TextEncoder().encode("data: ñ\n\n");
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes.slice(0, 7));
      controller.enqueue(bytes.slice(7));
      controller.close();
    },
  });
  const events: SseEvent[] = [];
  await readSse(stream, (event) => events.push(event));
  expect(events).toEqual([{ event: "message", data: "ñ" }]);
});

it("joins data lines, skips comments and events without data, keeps a trailing event", async () => {
  expect(await collect([": ping\n\nevent: a\n\nevent: b\ndata: 1\ndata: 2\r\n\r\nid: 3\ndata:x"])).toEqual([
    { event: "b", data: "1\n2" },
    { event: "message", data: "x" },
  ]);
});

it("rejects when the stream breaks", async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.error(new TypeError("network error"));
    },
  });
  await expect(readSse(stream, () => undefined)).rejects.toThrow("network error");
});
