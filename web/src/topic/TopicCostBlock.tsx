import { useEffect, useState } from "react";
import { describeFailure, formatDate, type ReadResult } from "../desk/api";
import { type CostTotals, fetchTopicCost, formatTokens, formatUsd, type TopicCost } from "../desk/costApi";

/**
 * "Coste" on the topic page (docs/VISION.md §5.13, #260): what the topic's Claude calls have cost
 * in total and per session, plus a row for the calls bound to no session (editor, generators),
 * from `GET /api/subjects/{s}/topics/{t}/cost`. Amounts are the backend's, labelled USD; calls of
 * a model with no known price add nothing to them, and a warning says the total falls short.
 * `refreshKey` reloads it (a generation or a preparation spends).
 */

export const UNPRICED_WARNING = "Hay llamadas sin precio conocido: el total se queda corto";

function calls(count: number): string {
  return count === 1 ? "1 llamada" : `${count} llamadas`;
}

function line(totals: CostTotals): string {
  return `${formatUsd(totals.usd)} · ${formatTokens(totals.tokens)} · ${calls(totals.calls)}`;
}

function CostList({ cost }: { cost: TopicCost }) {
  const rows = cost.sessions.map((session) => (
    <li key={session.session_id}>
      {session.started_at_ms === null ? `Sesión ${session.session_id}` : `Sesión del ${formatDate(session.started_at_ms)}`}
      {`: ${line(session)}`}
    </li>
  ));
  if (cost.no_session.calls > 0) {
    rows.push(<li key="no-session">{`Fuera de las sesiones (editor, material): ${line(cost.no_session)}`}</li>);
  }
  return (
    <>
      <p>Total del tema: {line(cost.total)}</p>
      {rows.length > 0 ? <ul aria-label="Coste por sesión">{rows}</ul> : <p>Todavía no hay gasto en este tema.</p>}
      {cost.total.unpriced_calls > 0 && <p role="note">{UNPRICED_WARNING}.</p>}
    </>
  );
}

export default function TopicCostBlock({
  subjectId,
  topicId,
  refreshKey = 0,
}: {
  subjectId: string;
  topicId: string;
  refreshKey?: number;
}) {
  const [cost, setCost] = useState<ReadResult<TopicCost> | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchTopicCost(subjectId, topicId).then((result) => {
      if (!cancelled) setCost(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, refreshKey]);

  return (
    <section aria-label="Coste">
      <h2>Coste</h2>
      {cost === null && <p>Cargando el coste…</p>}
      {cost !== null && cost.kind === "ok" && <CostList cost={cost.value} />}
      {cost !== null && cost.kind !== "ok" && <p>No se pudo cargar el coste: {describeFailure(cost)}</p>}
    </section>
  );
}
