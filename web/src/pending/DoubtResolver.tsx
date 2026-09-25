import { type FormEvent, useState } from "react";
import {
  type ActionResult,
  answerDoubt,
  type Doubt,
  type DoubtAnswer,
  describeActionFailure,
  dismissDoubt,
  type ResolutionResult,
} from "./doubts";

/** "Tus apuntes, página 3" for `sources/notes/page-003.jpg`; other sources by their file name. */
export function sourceLabel(sourceId: string): string {
  const page = /^sources\/(notes|book)\/page-0*(\d+)\./.exec(sourceId);
  if (page) return `${page[1] === "notes" ? "Tus apuntes" : "El libro"}, página ${page[2]}`;
  const pdf = /^sources\/pdf\/.*?(?:\.p0*(\d+))?\.[a-z]+$/.exec(sourceId);
  if (pdf) return pdf[1] ? `El PDF, página ${pdf[1]}` : "El PDF";
  if (sourceId.startsWith("sources/web/")) return "Una página web";
  if (sourceId.startsWith("sessions/")) return "Lo que dijiste en clase";
  return sourceId.slice(sourceId.lastIndexOf("/") + 1);
}

type Failure = Exclude<ActionResult<ResolutionResult>, { kind: "ok" }>;

export interface DoubtResolverProps {
  subjectId: string;
  topicId: string;
  doubt: Doubt;
  /** Another doubts operation of the page is running: every button waits. */
  disabled?: boolean;
  /** An operation started (`true`) or ended (`false`). */
  onBusy?: (busy: boolean) => void;
  /** The doubt was answered or dismissed. */
  onResolved: (result: ResolutionResult) => void;
  /** A refusal that means the queue changed under us (closed already, unknown): read it again. */
  onStale?: () => void;
}

/**
 * The answer form of one open doubt, shown in its card: the editor's question, its suggested
 * answers (one click answers), for a contradiction the source that is right (and whether to keep
 * a note of what the others say), free text, and "Descartar". Answers go to `POST .../answer`,
 * dismissing to `POST .../dismiss`. A refusal shows the backend's Spanish reason; a reached cost
 * cap offers "Continuar igualmente", which sends the same answer with `confirm_over_cap`.
 */
export default function DoubtResolver({
  subjectId,
  topicId,
  doubt,
  disabled = false,
  onBusy,
  onResolved,
  onStale,
}: DoubtResolverProps) {
  const { item, question } = doubt;
  const suggestions = question?.suggestions ?? [];
  const options = question?.options ?? [];
  const [text, setText] = useState("");
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [keepDiscarded, setKeepDiscarded] = useState(false);
  const [working, setWorking] = useState<"answer" | "dismiss" | null>(null);
  const [failure, setFailure] = useState<{ result: Failure; retry: (() => void) | null } | null>(null);

  const run = async (
    kind: "answer" | "dismiss",
    call: (confirm: boolean) => Promise<ActionResult<ResolutionResult>>,
    confirm = false,
  ) => {
    setWorking(kind);
    setFailure(null);
    onBusy?.(true);
    const result = await call(confirm);
    setWorking(null);
    onBusy?.(false);
    if (result.kind === "ok") {
      onResolved(result.value);
      return;
    }
    const overCap = result.kind === "refused" && result.overCap;
    setFailure({ result, retry: overCap ? () => void run(kind, call, true) : null });
    if (result.kind === "refused" && !overCap && (result.status === 404 || result.status === 409)) onStale?.();
  };

  const answer = (body: DoubtAnswer) =>
    void run("answer", (confirm) => answerDoubt(subjectId, topicId, item.id, body, confirm));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const free = text.trim();
    const body: DoubtAnswer = {};
    if (sourceId !== null) {
      body.source_id = sourceId;
      body.keep_discarded = keepDiscarded;
    }
    if (free !== "") body.answer = free;
    answer(body);
  };

  const busy = disabled || working !== null;
  const canSubmit = !busy && (text.trim() !== "" || sourceId !== null);

  return (
    <form className="doubt-resolver" aria-label="Resolver la duda" onSubmit={submit}>
      {question !== null ? (
        <p className="doubt-question">{question.question}</p>
      ) : (
        <p className="doubt-no-question">
          El editor todavía no ha preparado una pregunta para esta duda. Puedes contestar con tus palabras o
          descartarla.
        </p>
      )}
      {suggestions.length > 0 && (
        <div role="group" aria-label="Respuestas sugeridas" className="doubt-suggestions">
          {suggestions.map((suggestion, index) => (
            <button key={index} type="button" disabled={busy} onClick={() => answer({ suggestion: index + 1 })}>
              {suggestion}
            </button>
          ))}
        </div>
      )}
      {options.length > 0 && (
        <fieldset className="doubt-options" disabled={busy}>
          <legend>¿Qué fuente tiene razón?</legend>
          {options.map((option) => (
            <label key={option.source_id}>
              <input
                type="radio"
                name={`source-${item.id}`}
                value={option.source_id}
                checked={sourceId === option.source_id}
                onChange={() => setSourceId(option.source_id)}
              />
              {sourceLabel(option.source_id)}: «{option.says}»
            </label>
          ))}
          <label>
            <input
              type="checkbox"
              checked={keepDiscarded}
              disabled={sourceId === null}
              onChange={(event) => setKeepDiscarded(event.target.checked)}
            />
            Guardar también una nota con lo que dicen las otras
          </label>
        </fieldset>
      )}
      <label className="doubt-free">
        {options.length > 0 ? "Comentario (opcional)" : suggestions.length > 0 ? "O con tus palabras" : "Tu respuesta"}
        <textarea value={text} maxLength={2000} rows={2} disabled={busy} onChange={(e) => setText(e.target.value)} />
      </label>
      <div className="doubt-buttons">
        <button type="submit" disabled={!canSubmit}>
          Responder
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void run("dismiss", () => dismissDoubt(subjectId, topicId, item.id))}
        >
          Descartar
        </button>
      </div>
      <div aria-live="polite">
        {working === "answer" && <p>El editor está aplicando tu respuesta a los apuntes…</p>}
        {working === "dismiss" && <p>Descartando la duda…</p>}
      </div>
      {failure !== null && (
        <div role="alert" className="doubt-failure">
          <p>{describeActionFailure(failure.result)}</p>
          {failure.retry !== null && (
            <button type="button" disabled={busy} onClick={failure.retry}>
              Continuar igualmente
            </button>
          )}
        </div>
      )}
    </form>
  );
}
