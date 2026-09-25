import { describe, expect, it } from "vitest";
import { INITIAL_STATE, type LiveEvent, type LiveState, outlineTree, parseLiveEvent, reduceLive } from "./live";
import { SESSION, capture, snapshot } from "./testLive";

function fold(events: [string, unknown][], state: LiveState = INITIAL_STATE): LiveState {
  return events.reduce((current, [name, data]) => {
    const event = parseLiveEvent(name, JSON.stringify(data));
    return event === null ? current : reduceLive(current, event);
  }, state);
}

const seg = (id: string, t: number, text: string) => ({ segment_id: id, t_start: t, t_end: t + 900, text });

describe("parseLiveEvent", () => {
  it("reads every event the stream sends", () => {
    expect(parseLiveEvent("snapshot", JSON.stringify(snapshot()))).toEqual({ type: "snapshot", ...snapshot() });
    expect(parseLiveEvent("segment", JSON.stringify(seg("a", 0, "hola")))).toEqual({
      type: "segment",
      segment: seg("a", 0, "hola"),
    });
    expect(parseLiveEvent("capture", JSON.stringify(capture()))).toEqual({ type: "capture", capture: capture() });
    expect(parseLiveEvent("ended", '{"session_id": "s-1"}')).toEqual({ type: "ended", session_id: "s-1" });
  });

  it("drops what it cannot read instead of throwing", () => {
    expect(parseLiveEvent("segment", "not json")).toBeNull();
    expect(parseLiveEvent("segment", '{"text": "sin id"}')).toBeNull();
    expect(parseLiveEvent("capture", "[]")).toBeNull();
    expect(parseLiveEvent("mystery", "{}")).toBeNull();
    const lenient = parseLiveEvent(
      "snapshot",
      JSON.stringify({ session: SESSION, segments: [{ bad: 1 }, seg("a", 0, "x")], outline: "?", open_pending: "2" }),
    );
    expect(lenient).toMatchObject({ segments: [seg("a", 0, "x")], captures: [], outline: [], open_pending: 0 });
    expect(parseLiveEvent("capture", JSON.stringify(capture({ status: "weird" }))))
      .toMatchObject({ capture: { status: "pending" } });
  });
});

describe("reduceLive", () => {
  it("starts idle without a session and live with one", () => {
    expect(fold([["snapshot", snapshot({ session: null })]]).phase).toBe("idle");
    const live = fold([["snapshot", snapshot({ segments: [seg("a", 0, "hola")], open_pending: 2 })]]);
    expect(live).toMatchObject({ phase: "live", session: SESSION, openPending: 2 });
    expect(live.segments).toHaveLength(1);
  });

  it("replaces the partial with its final and ignores late or repeated ones", () => {
    const state = fold([
      ["snapshot", snapshot()],
      ["partial", seg("a", 0, "la velo")],
      ["segment", seg("a", 0, "la velocidad")],
      ["partial", seg("a", 0, "la velo")],
      ["segment", seg("a", 0, "la velocidad")],
      ["partial", seg("b", 1000, "es cons")],
    ]);
    expect(state.segments.map((s) => s.text)).toEqual(["la velocidad"]);
    expect(state.partial?.text).toBe("es cons");
  });

  it("adds and updates captures by id, and replaces the outline", () => {
    const state = fold([
      ["snapshot", snapshot({ captures: [capture()] })],
      ["capture", capture({ capture_id: "cap-2" })],
      ["capture", capture({ status: "transcribed", text: "v = cte", page_number: 1 })],
      ["outline", { outline: [{ section_id: "mru", title: "MRU", parent_id: null, segment_count: 1 }], open_pending: 3 }],
    ]);
    expect(state.captures.map((c) => [c.capture_id, c.status])).toEqual([
      ["cap-1", "transcribed"],
      ["cap-2", "pending"],
    ]);
    expect(state.outline.map((s) => s.title)).toEqual(["MRU"]);
    expect(state.openPending).toBe(3);
  });

  it("keeps the ended session on screen, also across a reconnect that finds none", () => {
    const ended = fold([
      ["snapshot", snapshot({ segments: [seg("a", 0, "hola")] })],
      ["partial", seg("b", 1000, "adi")],
      ["ended", { session_id: "other" }],
    ]);
    expect(ended.phase).toBe("live");
    const done = fold([["ended", { session_id: "s-1" }]], ended);
    expect(done).toMatchObject({ phase: "ended", partial: null });
    expect(fold([["segment", seg("c", 2000, "tarde")]], done).segments).toHaveLength(1);
    const reconnected = fold([["snapshot", snapshot({ session: null })]], done);
    expect(reconnected.phase).toBe("ended");
    expect(reconnected.segments).toHaveLength(1);
    const next = fold([["snapshot", snapshot({ session: { ...SESSION, session_id: "s-2" } })]], reconnected);
    expect(next).toMatchObject({ phase: "live", segments: [] });
  });

  it("marks a session that vanished without `ended` (backend restarted) as ended", () => {
    const state = fold([
      ["snapshot", snapshot()],
      ["snapshot", snapshot({ session: null })],
    ]);
    expect(state.phase).toBe("ended");
  });
});

it("outlineTree nests sections under their parents, keeping orphans at the top", () => {
  const section = (section_id: string, parent_id: string | null) => ({ section_id, title: section_id, parent_id, segment_count: 0 });
  const tree = outlineTree([section("a", null), section("a1", "a"), section("b", null), section("x", "missing")]);
  expect(tree.map((n) => [n.section.section_id, n.children.map((c) => c.section.section_id)])).toEqual([
    ["a", ["a1"]],
    ["b", []],
    ["x", []],
  ]);
});

// Type-level check that the reducer covers every event kind.
const _exhaustive: (event: LiveEvent) => LiveState = (event) => reduceLive(INITIAL_STATE, event);
void _exhaustive;
