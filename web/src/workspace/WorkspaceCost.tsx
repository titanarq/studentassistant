import { useEffect, useState } from "react";
import { type CostStatus, fetchCostStatus, fetchTopicCost, formatUsd } from "../desk/costApi";
import { PAUSED_BANNER } from "../desk/DeskCost";
import { UNPRICED_WARNING } from "../topic/TopicCostBlock";

/**
 * The spend line of the workspace header (docs/VISION.md §5.13, #372): "Esta sesión: <importe>"
 * while a capture session of the topic is open (`GET /api/cost` with the session), else
 * "Este tema: <importe>" (`GET .../cost`). When a cap is reached (the status "Gasto de hoy" shows
 * on the desk) a badge carries the desk's wording; calls with no known price put
 * `UNPRICED_WARNING` in the line's `title`. `refreshKey` reads it again (the workspace bumps it
 * when the document changed after a turn). Nothing shows while loading or when a read fails.
 */

interface Shown {
  label: string;
  usd: number;
  unpriced: number;
  paused: boolean;
}

function isPaused(status: CostStatus): boolean {
  return status.observer_paused || status.editor_needs_confirmation;
}

async function readCost(subjectId: string, topicId: string, sessionId: string | null): Promise<Shown | null> {
  if (sessionId !== null) {
    const status = await fetchCostStatus({ subjectId, topicId, sessionId });
    if (status.kind !== "ok") return null;
    const { session_usd, unpriced_session_calls } = status.value;
    return { label: "Esta sesión", usd: session_usd, unpriced: unpriced_session_calls, paused: isPaused(status.value) };
  }
  const [cost, status] = await Promise.all([fetchTopicCost(subjectId, topicId), fetchCostStatus()]);
  if (cost.kind !== "ok") return null;
  return {
    label: "Este tema",
    usd: cost.value.total.usd,
    unpriced: cost.value.total.unpriced_calls,
    paused: status.kind === "ok" && isPaused(status.value),
  };
}

export default function WorkspaceCost({
  subjectId,
  topicId,
  sessionId,
  refreshKey,
}: {
  subjectId: string;
  topicId: string;
  /** The open capture session of this topic, or null. */
  sessionId: string | null;
  refreshKey: string | number;
}) {
  const [shown, setShown] = useState<Shown | null>(null);

  useEffect(() => {
    let cancelled = false;
    void readCost(subjectId, topicId, sessionId).then((result) => {
      if (!cancelled) setShown(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, sessionId, refreshKey]);

  if (shown === null) return null;
  return (
    <p className="workspace-cost" aria-label="Gasto">
      <span title={shown.unpriced > 0 ? UNPRICED_WARNING : undefined}>
        {`${shown.label}: ${formatUsd(shown.usd)}`}
      </span>
      {shown.paused && (
        <span className="workspace-cost-cap" role="alert">
          {PAUSED_BANNER}
        </span>
      )}
    </p>
  );
}
