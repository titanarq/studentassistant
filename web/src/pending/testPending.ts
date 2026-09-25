import type { PendingItem, TopicPending } from "./api";
import type { Doubt, DoubtQuestion, DoubtsQueue } from "./doubts";

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

export function question(overrides: Partial<DoubtQuestion> = {}): DoubtQuestion {
  return {
    pending_id: "p1",
    question: "¿Qué fecha pone en la página 1?",
    suggestions: ["14 de julio de 1789", "14 de julio de 1791"],
    options: [],
    asked_at: { session_id: "2026-09-24-1100", seq: 2 },
    ...overrides,
  };
}

export function doubt(itemOverrides: Partial<PendingItem> = {}, withQuestion: DoubtQuestion | null = null): Doubt {
  return { item: item(itemOverrides), question: withQuestion, outcome: null };
}

export function doubts(items: Doubt[], current?: string | null): DoubtsQueue {
  const open = items.filter((d) => d.item.status === "open");
  return {
    subject: "historia",
    topic: "revolucion-francesa",
    open_count: open.length,
    current: current === undefined ? (open[0]?.item.id ?? null) : current,
    items,
  };
}

export function resolution(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    subject: "historia",
    topic: "revolucion-francesa",
    pending_id: "p1",
    status: "resolved",
    resolution: "La fecha es el 14 de julio de 1789.",
    notes_changed: true,
    session_id: "2026-09-25-0900",
    commit: "abc123",
    attempts: 1,
    warning: null,
    model: "claude-opus",
    ...overrides,
  };
}
