import { expect, it } from "vitest";
import { feedbackLabel, readFeedback } from "./feedback";

it("reads a turn's recorded app feedback, and nothing from a missing or unknown one (#472)", () => {
  expect(readFeedback({ id: "fb-1", kind: "mejora", title: "Exportar a PDF", extra: 1 })).toEqual({
    id: "fb-1",
    kind: "mejora",
    title: "Exportar a PDF",
  });
  expect(readFeedback({ id: "fb-2", kind: "bug" })).toEqual({ id: "fb-2", kind: "bug", title: "" });
  expect(readFeedback(undefined)).toBeNull();
  expect(readFeedback(null)).toBeNull();
  expect(readFeedback({ id: "fb-3", kind: "otro", title: "x" })).toBeNull();
  expect(readFeedback({ id: "", kind: "bug", title: "x" })).toBeNull();
});

it("labels the chip in Spanish by kind", () => {
  expect(feedbackLabel({ id: "a", kind: "bug", title: "" })).toBe("Bug apuntado");
  expect(feedbackLabel({ id: "b", kind: "mejora", title: "" })).toBe("Mejora apuntada");
});
