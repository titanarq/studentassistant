import { useEffect, useState } from "react";
import { describeFailure, type ReadResult } from "../desk/api";
import { fetchPracticeSummary, formatDue, type PracticeSummary, type TopicPractice } from "../desk/practiceSummary";

/** "6 para repasar · 4 nuevas" for a topic with something to review now. */
export function reviewText(topic: TopicPractice): string {
  return `${topic.due} para repasar · ${topic.new} ${topic.new === 1 ? "nueva" : "nuevas"}`;
}

/** "Nada que repasar hoy." with the next review, when the topic has one. */
export function nothingText(topic: TopicPractice | undefined): string {
  return topic?.next_due == null
    ? "Nada que repasar hoy."
    : `Nada que repasar hoy. Próximo repaso: ${formatDue(topic.next_due)}.`;
}

/**
 * "Repasos para hoy" of one topic (#333): its entry of `GET /api/practice/summary` (#280, the
 * study desk's client `desk/practiceSummary.ts`). With something to review, "Repasar ahora" opens
 * the **Tarjetas de memoria** option (`onReview`).
 */
export default function ReviewsToday({
  subjectId,
  topicId,
  onReview,
}: {
  subjectId: string;
  topicId: string;
  onReview: (trigger: HTMLElement) => void;
}) {
  const [summary, setSummary] = useState<ReadResult<PracticeSummary> | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchPracticeSummary().then((result) => {
      if (!cancelled) setSummary(result);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const topic =
    summary?.kind === "ok"
      ? summary.value.topics.find((t) => t.subject_id === subjectId && t.topic_id === topicId)
      : undefined;
  const pending = topic !== undefined && topic.due + topic.new > 0;

  return (
    <section className="study-reviews" aria-labelledby="study-reviews-heading">
      <h2 id="study-reviews-heading">Repasos para hoy</h2>
      {summary === null && <p>Cargando los repasos…</p>}
      {summary !== null && summary.kind !== "ok" && (
        <p>No se pudieron leer los repasos: {describeFailure(summary)}</p>
      )}
      {summary?.kind === "ok" && (
        <p>
          {pending ? reviewText(topic) : nothingText(topic)}
          {pending && (
            <>
              {" "}
              <button type="button" className="study-review-now" onClick={(event) => onReview(event.currentTarget)}>
                Repasar ahora
              </button>
            </>
          )}
        </p>
      )}
    </section>
  );
}
