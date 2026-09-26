import { useEffect, useState } from "react";
import { describeFailure, type ReadResult, topicPath } from "./api";
import {
  countsText,
  fetchPracticeSummary,
  formatDue,
  nextDue,
  type PracticeSummary,
  topicsToReview,
} from "./practiceSummary";

/**
 * "Repasos para hoy" on the study desk (#285), from `GET /api/practice/summary`: the totals and
 * one row per topic with due or new items, each a link to the topic's practice page. With
 * practice material but nothing to review it says so and gives the next due date; without any
 * practice material the block is not shown. A failed read is one discreet Spanish line.
 */

function Warnings({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <ul aria-label="Avisos de los repasos">
      {warnings.map((warning, index) => (
        <li key={index}>{warning}</li>
      ))}
    </ul>
  );
}

function Summary({ summary }: { summary: PracticeSummary }) {
  const rows = topicsToReview(summary);
  if (rows.length === 0) {
    const next = nextDue(summary);
    return (
      <>
        <p>
          Nada que repasar hoy.
          {next !== null && ` Próximo repaso: ${formatDue(next)}.`}
        </p>
        <Warnings warnings={summary.warnings} />
      </>
    );
  }
  const topicCount = rows.length === 1 ? "1 tema" : `${rows.length} temas`;
  return (
    <>
      <p>
        {countsText(summary.totals.due, summary.totals.new)} en {topicCount}.
      </p>
      <ul>
        {rows.map((topic) => (
          <li key={`${topic.subject_id}/${topic.topic_id}`}>
            <a href={`${topicPath(topic.subject_id, topic.topic_id)}/practice`}>
              {topic.subject_name} · {topic.topic_title}
            </a>{" "}
            — {countsText(topic.due, topic.new)}
          </li>
        ))}
      </ul>
      <Warnings warnings={summary.warnings} />
    </>
  );
}

export default function DeskPractice() {
  const [result, setResult] = useState<ReadResult<PracticeSummary> | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchPracticeSummary().then((answer) => {
      if (!cancelled) setResult(answer);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (result === null) return null;
  if (result.kind === "ok" && result.value.topics.length === 0 && result.value.warnings.length === 0) return null;
  return (
    <section className="desk-block desk-practice" aria-label="Repasos para hoy">
      <h2>Repasos para hoy</h2>
      {result.kind === "ok" ? (
        result.value.topics.length === 0 ? (
          <Warnings warnings={result.value.warnings} />
        ) : (
          <Summary summary={result.value} />
        )
      ) : (
        <p>No se pudieron cargar los repasos: {describeFailure(result)}</p>
      )}
    </section>
  );
}
