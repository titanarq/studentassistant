/**
 * Session health on the capture page (#262): the backend counts what failed behind the scenes
 * during the session -- observer calls, page transcriptions, vault pushes -- and whether the
 * observer is paused by a cost cap (`GET /api/sessions/{id}/health`, docs/modules/server.md).
 * The page asks every `HEALTH_POLL_MS` while the session runs and shows one discreet Spanish line
 * per problem, nothing at all while the session is healthy, and never a modal.
 *
 * A web-only route, not phone protocol: its body is checked here by hand, and anything that is
 * not the expected shape is treated as "no news" rather than shown.
 */

import { useEffect, useState } from "react";
import { SESSIONS_PATH } from "./api";

/** How often the page asks: often enough to notice within a minute, rare enough to be free. */
export const HEALTH_POLL_MS = 15_000;

export interface FailureSummary {
  count: number;
  message: string | null;
}

export interface SessionHealth {
  session_id: string;
  ok: boolean;
  observer: FailureSummary;
  observer_paused: boolean;
  observer_paused_message: string | null;
  transcription: FailureSummary;
  push: FailureSummary;
}

export function sessionHealthPath(sessionId: string): string {
  return `${SESSIONS_PATH}/${encodeURIComponent(sessionId)}/health`;
}

function isFailure(value: unknown): value is FailureSummary {
  if (typeof value !== "object" || value === null) return false;
  const { count, message } = value as Record<string, unknown>;
  return typeof count === "number" && (message === null || typeof message === "string");
}

function isHealth(value: unknown): value is SessionHealth {
  if (typeof value !== "object" || value === null) return false;
  const body = value as Record<string, unknown>;
  return (
    typeof body.ok === "boolean" &&
    typeof body.observer_paused === "boolean" &&
    isFailure(body.observer) &&
    isFailure(body.transcription) &&
    isFailure(body.push)
  );
}

/**
 * One ask: the summary, `"gone"` when the backend no longer knows the session (404: stop asking),
 * or `null` when there is no answer worth showing this time (unreachable, an error, another shape).
 */
export async function fetchSessionHealth(sessionId: string): Promise<SessionHealth | "gone" | null> {
  try {
    const response = await fetch(sessionHealthPath(sessionId), { method: "GET" });
    if (response.status === 404) return "gone";
    if (!response.ok) return null;
    const body: unknown = await response.json();
    return isHealth(body) ? body : null;
  } catch {
    return null;
  }
}

function times(count: number): string {
  return count === 1 ? "1 vez" : `${count} veces`;
}

/** The Spanish lines of a summary: none for a healthy session. */
export function healthLines(health: SessionHealth | null): string[] {
  if (health === null) return [];
  const lines: string[] = [];
  const { observer, transcription, push } = health;
  if (observer.count > 0) {
    lines.push(`El observador ha fallado ${times(observer.count)}: ${observer.message ?? "error"}.`);
  }
  if (health.observer_paused) {
    const why = health.observer_paused_message ?? "se ha alcanzado un límite de gasto";
    lines.push(`El observador está en pausa: ${why}.`);
  }
  if (transcription.count > 0) {
    const pages = transcription.count === 1 ? "1 página" : `${transcription.count} páginas`;
    lines.push(`No se han podido transcribir ${pages}: ${transcription.message ?? "error"}.`);
  }
  if (push.count > 0) {
    lines.push(
      `La bóveda no se ha podido subir a GitHub (${times(push.count)}): ${push.message ?? "error"}.`,
    );
  }
  return lines;
}

/**
 * Asks for the session's health every `intervalMs` while `active`, one ask at a time, and answers
 * the lines to show. The first ask waits one interval; a 404 ends the asking; turning `active`
 * off (the session ends) or unmounting stops the timer.
 */
export function useSessionHealth(
  sessionId: string,
  active: boolean,
  intervalMs: number = HEALTH_POLL_MS,
): string[] {
  const [health, setHealth] = useState<SessionHealth | null>(null);
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const ask = async (): Promise<void> => {
      const answer = await fetchSessionHealth(sessionId);
      if (stopped) return;
      if (answer === "gone") {
        setHealth(null);
        return;
      }
      if (answer !== null) setHealth(answer);
      timer = setTimeout(() => void ask(), intervalMs);
    };
    timer = setTimeout(() => void ask(), intervalMs);
    return () => {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [sessionId, active, intervalMs]);
  return active ? healthLines(health) : [];
}
