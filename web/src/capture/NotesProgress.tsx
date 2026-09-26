/**
 * What the capture page shows after "Terminar y preparar apuntes" (#271): the session has ended
 * with `prepare_notes: true` (protocol 1.6, #258) and the backend prepares the topic's notes in the
 * background. This view follows that generation through `GET .../notes/generation` every
 * `NOTES_POLL_MS` until it reaches a final status or the view goes away, and says in Spanish how it
 * went, with a link to where the student goes next: the notes when they are ready, the topic page
 * (where **Prepárame el tema** retries or confirms a cost cap) otherwise.
 *
 * A poll that got no answer (the backend unreachable, a 5xx) keeps polling and says so under the
 * running line; a refusal (an unknown topic, a 4xx) or a body off the protocol stops for good.
 */

import { useEffect, useState } from "react";
import type { NotesGenerationStart, NotesGenerationStatus } from "../protocol";
import { topicPath } from "../desk/api";
import { fetchNotesGeneration, type NotesGenerationResult } from "./api";
import { describeFailure, type ApiFailure } from "./failures";
import "./capture.css";

/** Opus takes minutes: a poll every few seconds is plenty. */
export const NOTES_POLL_MS = 3_000;

/** Where the view stands; every kind but `running` is final and stops the polling. */
export type NotesProgressState =
  | { kind: "running"; pollFailure: ApiFailure | null }
  | { kind: "done"; version: number | null; draft: boolean; warning: string | null }
  | { kind: "failed"; detail: string | null }
  | { kind: "needs_confirmation"; detail: string | null }
  /** The backend cannot prepare notes (no Claude, or an older backend that ignored the flag). */
  | { kind: "unavailable" }
  /** The backend knows of no generation of the topic (`idle`: it restarted meanwhile). */
  | { kind: "lost" }
  /** Polling was refused for good. */
  | { kind: "unknown"; failure: ApiFailure };

/** What the end response's `notes_generation` means before the first poll. */
export function progressFromStart(start: NotesGenerationStart | undefined): NotesProgressState {
  return start === "started" || start === "running"
    ? { kind: "running", pollFailure: null }
    : { kind: "unavailable" };
}

export function progressFromStatus(status: NotesGenerationStatus): NotesProgressState {
  switch (status.status) {
    case "running":
      return { kind: "running", pollFailure: null };
    case "done":
      return {
        kind: "done",
        version: status.version ?? null,
        draft: status.draft === true,
        warning: status.warning ?? null,
      };
    case "failed":
      return { kind: "failed", detail: status.detail ?? null };
    case "needs_confirmation":
      return { kind: "needs_confirmation", detail: status.detail ?? null };
    case "idle":
      return { kind: "lost" };
  }
}

/** A failure worth another try: no answer at all, or the server's own error. */
function transient(failure: ApiFailure): boolean {
  return failure.kind === "unreachable" || (failure.kind === "error" && failure.status >= 500);
}

export function progressFromResult(result: NotesGenerationResult): NotesProgressState {
  if (result.kind === "ok") return progressFromStatus(result.value);
  return transient(result)
    ? { kind: "running", pollFailure: result }
    : { kind: "unknown", failure: result };
}

export interface NotesProgressProps {
  subjectId: string;
  topicId: string;
  subjectName: string;
  topicName: string;
  /** The end response's `notes_generation`. */
  start: NotesGenerationStart | undefined;
  /** "Volver": back to the picker. */
  onClose?: () => void;
  intervalMs?: number;
}

export default function NotesProgress({
  subjectId,
  topicId,
  subjectName,
  topicName,
  start,
  onClose,
  intervalMs = NOTES_POLL_MS,
}: NotesProgressProps) {
  const [progress, setProgress] = useState<NotesProgressState>(() => progressFromStart(start));
  const following = progress.kind === "running";

  useEffect(() => {
    if (!following) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async (): Promise<void> => {
      timer = null;
      const next = progressFromResult(await fetchNotesGeneration(subjectId, topicId));
      if (disposed) return;
      setProgress(next);
      if (next.kind === "running") timer = setTimeout(() => void poll(), intervalMs);
    };
    timer = setTimeout(() => void poll(), intervalMs);
    return () => {
      disposed = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [following, intervalMs, subjectId, topicId]);

  const topicHref = topicPath(subjectId, topicId);
  const notesHref = `${topicHref}/notes`;

  return (
    <main className="capture-page capture-progress">
      <header className="capture-header">
        <h1>Preparar los apuntes</h1>
        <p className="page-context">
          {subjectName} · {topicName}
        </p>
        <p>La sesión se ha terminado.</p>
      </header>

      <section className="capture-step" aria-label="Preparación de los apuntes">
        {progress.kind === "running" && (
          <>
            <p role="status">Preparando los apuntes… Puede tardar unos minutos.</p>
            {progress.pollFailure !== null && (
              <p role="alert">
                {describeFailure("No se puede consultar el progreso", progress.pollFailure)} Se
                volverá a intentar.
              </p>
            )}
          </>
        )}
        {progress.kind === "done" && (
          <>
            <p role="status">
              {progress.draft
                ? "El borrador de los apuntes está listo, pero todavía no es la versión definitiva: revísalo antes de darlo por bueno."
                : progress.version !== null
                  ? `Los apuntes están listos (versión ${progress.version}).`
                  : "Los apuntes están listos."}
            </p>
            {progress.warning !== null && <p role="alert">{progress.warning}</p>}
            <a href={notesHref}>{progress.draft ? "Revisar el borrador" : "Abrir los apuntes"}</a>
          </>
        )}
        {progress.kind === "failed" && (
          <>
            <p role="alert">
              {progress.detail !== null
                ? `No se pudieron preparar los apuntes: ${progress.detail}`
                : "No se pudieron preparar los apuntes."}
            </p>
            <a href={topicHref}>Ir al tema para volver a intentarlo</a>
          </>
        )}
        {progress.kind === "needs_confirmation" && (
          <>
            <p role="alert">
              {progress.detail ?? "Se ha alcanzado el límite de gasto y no se ha gastado nada."}
            </p>
            <p>
              Para prepararlos igualmente, confírmalo con «Prepárame el tema» en la página del tema.
            </p>
            <a href={topicHref}>Ir al tema</a>
          </>
        )}
        {progress.kind === "unavailable" && (
          <>
            <p role="alert">
              El servidor no puede preparar los apuntes ahora. La sesión se ha terminado igualmente.
            </p>
            <a href={topicHref}>Ir al tema</a>
          </>
        )}
        {progress.kind === "lost" && (
          <>
            <p role="alert">
              El servidor ya no tiene noticia de la preparación (quizá se reinició). Puedes
              prepararlos desde la página del tema.
            </p>
            <a href={topicHref}>Ir al tema</a>
          </>
        )}
        {progress.kind === "unknown" && (
          <>
            <p role="alert">
              {describeFailure("No se puede consultar la preparación de los apuntes", progress.failure)}
            </p>
            <a href={topicHref}>Ir al tema</a>
          </>
        )}
      </section>

      <button type="button" onClick={() => onClose?.()}>
        Volver
      </button>
    </main>
  );
}
