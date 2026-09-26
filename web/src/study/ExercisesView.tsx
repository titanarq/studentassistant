import { useEffect, useRef, useState } from "react";
import { type ActionResult, describeActionFailure } from "../pending/doubts";
import { type ExamQuestion, fetchExam, type StoredExam, WHOLE_QUESTION } from "../exam/api";

/**
 * **Ejercicios** of the study screen (#333): the practice exercises of `examen.yaml` (`fetchExam`)
 * one at a time, "Ver solución" revealing the worked solution and its criteria, "Anterior" /
 * "Siguiente" moving between them. The exercise shown reports its anchors (`onFocusAnchors`),
 * and its "En los apuntes:" chips report their one anchor.
 */
export default function ExercisesView({
  subjectId,
  topicId,
  onFocusAnchors,
  anchorLabel,
}: {
  subjectId: string;
  topicId: string;
  onFocusAnchors?: (anchors: string[]) => void;
  anchorLabel?: (anchor: string) => string;
}) {
  const [exam, setExam] = useState<ActionResult<StoredExam> | null>(null);
  const [index, setIndex] = useState(0);
  const [revealed, setRevealed] = useState(false);
  const focusRef = useRef(onFocusAnchors);
  focusRef.current = onFocusAnchors;

  useEffect(() => {
    let cancelled = false;
    void fetchExam(subjectId, topicId).then((result) => {
      if (cancelled) return;
      setExam(result);
      setIndex(0);
      setRevealed(false);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  const exercises: ExamQuestion[] = exam?.kind === "ok" ? exam.value.exercises : [];
  const current = exercises[index];

  useEffect(() => {
    if (exam !== null) focusRef.current?.(current?.anchors ?? []);
  }, [exam, current]);

  if (exam === null) return <p>Cargando los ejercicios…</p>;
  if (exam.kind !== "ok") return <p role="alert">{describeActionFailure(exam)}</p>;
  if (current === undefined) return <p>Este examen no trae ejercicios de práctica.</p>;

  const go = (next: number) => {
    setIndex(next);
    setRevealed(false);
  };

  return (
    <div className="study-exercises">
      <p className="study-exercise-progress">
        Ejercicio {index + 1} de {exercises.length}
      </p>
      <article className="study-exercise" aria-label={`Ejercicio ${index + 1}`}>
        <p className="study-exercise-statement">{current.statement}</p>
        <button type="button" aria-expanded={revealed} onClick={() => setRevealed((value) => !value)}>
          {revealed ? "Ocultar solución" : "Ver solución"}
        </button>
        {revealed && (
          <div className="study-exercise-solution" role="group" aria-label="Solución">
            <h3>Solución</h3>
            <p>{current.solution}</p>
            {current.criteria.length > 0 && current.criteria[0].criterion !== WHOLE_QUESTION && (
              <>
                <h3>Criterios de corrección</h3>
                <ul>
                  {current.criteria.map((criterion, n) => (
                    <li key={n}>{criterion.criterion}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
        {current.anchors.length > 0 && (
          <p className="practice-sources practice-chips">
            En los apuntes:{" "}
            {current.anchors.map((anchor) => (
              <button
                key={anchor}
                type="button"
                className="practice-chip"
                onClick={() => onFocusAnchors?.([anchor])}
              >
                § {anchorLabel?.(anchor) ?? anchor}
              </button>
            ))}
          </p>
        )}
      </article>
      <p className="study-exercise-nav">
        <button type="button" disabled={index === 0} onClick={() => go(index - 1)}>
          Anterior
        </button>{" "}
        <button type="button" disabled={index >= exercises.length - 1} onClick={() => go(index + 1)}>
          Siguiente
        </button>
      </p>
    </div>
  );
}
