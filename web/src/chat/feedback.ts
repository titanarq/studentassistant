/**
 * App feedback a chat turn recorded (#472): the editor's `report_feedback` tool puts a bug report
 * or an improvement request about the app itself in the vault's feedback inbox, and the turn's
 * result (live and in the history) names it as `feedback: {id, kind, title}`. The workspace chat
 * and the study chat show it as a small chip on that turn.
 */

export interface FeedbackRef {
  id: string;
  kind: "bug" | "mejora";
  title: string;
}

type Json = Record<string, unknown>;

const isObject = (value: unknown): value is Json => typeof value === "object" && value !== null && !Array.isArray(value);

/** A turn's `feedback`, or null when it recorded none (or the body is not one the page reads). */
export function readFeedback(value: unknown): FeedbackRef | null {
  if (!isObject(value) || typeof value.id !== "string" || value.id === "") return null;
  if (value.kind !== "bug" && value.kind !== "mejora") return null;
  return { id: value.id, kind: value.kind, title: typeof value.title === "string" ? value.title : "" };
}

/** The chip's label: «Bug apuntado» or «Mejora apuntada». */
export function feedbackLabel(feedback: FeedbackRef): string {
  return feedback.kind === "bug" ? "Bug apuntado" : "Mejora apuntada";
}
