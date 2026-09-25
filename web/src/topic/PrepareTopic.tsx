import { useState } from "react";
import { topicPath } from "../desk/api";
import {
  type ActionResult,
  describeActionFailure,
  describeReview,
  postAction,
  type ReviewResult,
  reviewDoubts,
} from "../pending/doubts";

/** What the panel uses of the editor's `GenerationResult` (`POST .../notes/generate`). */
export interface GeneratedNotes {
  draft: boolean;
  version: number | null;
  warning: string | null;
}

function readGeneration(body: unknown): GeneratedNotes | null {
  if (typeof body !== "object" || body === null) return null;
  const b = body as Record<string, unknown>;
  if (typeof b.draft !== "boolean") return null;
  return {
    draft: b.draft,
    version: typeof b.version === "number" ? b.version : null,
    warning: typeof b.warning === "string" && b.warning !== "" ? b.warning : null,
  };
}

/** `POST /api/subjects/{s}/topics/{t}/notes/generate`: "prepárame el tema". */
export function generateNotes(
  subjectId: string,
  topicId: string,
  confirmOverCap = false,
): Promise<ActionResult<GeneratedNotes>> {
  return postAction(
    `/api${topicPath(subjectId, topicId)}/notes/generate`,
    { confirm_over_cap: confirmOverCap },
    readGeneration,
  );
}

type Step = "generate" | "review";
type Failure = { step: Step; result: Exclude<ActionResult<unknown>, { kind: "ok" }> };

/**
 * "Prepárame el tema" on the topic page: the editor writes the notes (`POST .../notes/generate`)
 * and then, as the doubts API asks of the web, reviews the topic's open doubts (`POST
 * .../doubts/review`), so the pending panel has its questions ready. A draft that did not pass the
 * validator is not reviewed. A reached cost cap offers "Continuar igualmente", which repeats the
 * step that stopped with `confirm_over_cap`; `onDone` runs after anything was written.
 */
export default function PrepareTopic({
  subjectId,
  topicId,
  onDone,
}: {
  subjectId: string;
  topicId: string;
  onDone?: () => void;
}) {
  const [working, setWorking] = useState<Step | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [failure, setFailure] = useState<Failure | null>(null);

  const review = async (confirm: boolean, before: string[]) => {
    setWorking("review");
    const result: ActionResult<ReviewResult> = await reviewDoubts(subjectId, topicId, confirm);
    setWorking(null);
    if (result.kind === "ok") {
      setLines([...before, describeReview(result.value), ...(result.value.warning ? [result.value.warning] : [])]);
    } else {
      setLines(before);
      setFailure({ step: "review", result });
    }
    onDone?.();
  };

  const generate = async (confirm: boolean) => {
    setWorking("generate");
    setFailure(null);
    setLines([]);
    const result = await generateNotes(subjectId, topicId, confirm);
    if (result.kind !== "ok") {
      setWorking(null);
      setFailure({ step: "generate", result });
      return;
    }
    const { draft, version, warning } = result.value;
    if (draft) {
      setWorking(null);
      setLines([`El editor ha dejado un borrador que no pasa la revisión.${warning ? ` ${warning}` : ""}`]);
      onDone?.();
      return;
    }
    const done = [version !== null ? `Apuntes v${version} listos.` : "Apuntes listos.", ...(warning ? [warning] : [])];
    setLines(done);
    await review(false, done);
  };

  const retry = () => {
    if (failure === null) return;
    const step = failure.step;
    setFailure(null);
    if (step === "generate") void generate(true);
    else void review(true, lines);
  };

  const overCap = failure !== null && failure.result.kind === "refused" && failure.result.overCap;

  return (
    <section aria-label="Prepárame el tema" className="prepare-topic">
      <button type="button" disabled={working !== null} onClick={() => void generate(false)}>
        Prepárame el tema
      </button>
      <div aria-live="polite">
        {working === "generate" && <p>El editor está escribiendo los apuntes…</p>}
        {working === "review" && <p>El editor está revisando las dudas…</p>}
        {lines.map((line) => (
          <p key={line}>{line}</p>
        ))}
        {lines.length > 0 && working === null && (
          <p>
            <a href={`${topicPath(subjectId, topicId)}/pending`}>Ver las dudas</a>
          </p>
        )}
      </div>
      {failure !== null && (
        <div role="alert">
          <p>
            {failure.step === "generate"
              ? "No se pudieron preparar los apuntes: "
              : "No se pudieron revisar las dudas: "}
            {describeActionFailure(failure.result)}
          </p>
          {overCap && (
            <button type="button" onClick={retry}>
              Continuar igualmente
            </button>
          )}
        </div>
      )}
    </section>
  );
}
