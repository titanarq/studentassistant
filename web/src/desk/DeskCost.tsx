import { useEffect, useState } from "react";
import { describeFailure, type ReadResult } from "./api";
import { type ActiveSession, type CostStatus, fetchCostStatus, formatUsd } from "./costApi";

/**
 * "Gasto de hoy" on the study desk (docs/VISION.md §5.13, #260): today's (UTC) spend against the
 * daily cap and, while a session is open, the session's against the session cap, from
 * `GET /api/cost`. Once a cap is reached the observer is paused and the editor asks before each
 * call: a banner says so. Caps are changed in the configuration file only.
 */

export const PAUSED_BANNER =
  "Se ha alcanzado el límite de gasto: el observador está en pausa y el editor pedirá confirmación antes de cada llamada.";

function against(spent: number, cap: number | null): string {
  return cap === null ? `${formatUsd(spent)} (sin límite)` : `${formatUsd(spent)} de ${formatUsd(cap)}`;
}

function StatusLines({ status, session }: { status: CostStatus; session?: ActiveSession }) {
  const paused = status.observer_paused || status.editor_needs_confirmation;
  const unpriced = status.unpriced_day_calls + (session === undefined ? 0 : status.unpriced_session_calls);
  return (
    <>
      {paused && <p role="alert">{PAUSED_BANNER}</p>}
      <p>Hoy (UTC): {against(status.day_usd, status.max_usd_per_day)}</p>
      {session !== undefined && (
        <p>Sesión abierta: {against(status.session_usd, status.max_usd_per_session)}</p>
      )}
      {unpriced > 0 && <p role="note">Hay llamadas sin precio conocido: el gasto real es mayor.</p>}
    </>
  );
}

export default function DeskCost({ session }: { session?: ActiveSession }) {
  const [status, setStatus] = useState<ReadResult<CostStatus> | null>(null);
  const subjectId = session?.subjectId;
  const topicId = session?.topicId;
  const sessionId = session?.sessionId;

  useEffect(() => {
    let cancelled = false;
    const active =
      subjectId !== undefined && topicId !== undefined && sessionId !== undefined
        ? { subjectId, topicId, sessionId }
        : undefined;
    fetchCostStatus(active).then((result) => {
      if (!cancelled) setStatus(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, sessionId]);

  return (
    <section className="desk-block desk-cost" aria-label="Gasto de hoy">
      <h2>Gasto de hoy</h2>
      {status === null && <p>Cargando el gasto…</p>}
      {status !== null && status.kind === "ok" && <StatusLines status={status.value} session={session} />}
      {status !== null && status.kind !== "ok" && <p>No se pudo cargar el gasto: {describeFailure(status)}</p>}
    </section>
  );
}
