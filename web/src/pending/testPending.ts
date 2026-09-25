import type { PendingItem, TopicPending } from "./api";

export function item(overrides: Partial<PendingItem> = {}): PendingItem {
  return {
    id: "p1",
    kind: "illegible",
    text: "No se lee la fecha de la toma de la Bastilla.",
    refs: { pages: [], segments: [], sources: [] },
    created_by: "observer",
    status: "open",
    resolution: null,
    added_at: { session_id: "2026-09-24-1030", seq: 4 },
    resolved_at: null,
    merged_ids: [],
    ...overrides,
  };
}

export function queue(items: PendingItem[], openCount?: number): TopicPending {
  return {
    subject_id: "historia",
    topic_id: "revolucion-francesa",
    open_count: openCount ?? items.filter((i) => i.status === "open").length,
    items,
  };
}
