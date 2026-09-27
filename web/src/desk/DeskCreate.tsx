import { type FormEvent, useState } from "react";
import { type ApiResult, createSubject, createTopic } from "../capture/api";
import { describeFailure } from "../capture/failures";
import type { Subject, Topic } from "../protocol";

/**
 * Creating on the study desk (#368): "Nueva asignatura" (one name field and **Crear**) and, per
 * subject, "Nuevo tema" (a small toggle that shows the same form). They post through the capture
 * page's own client (`capture/api.ts` `createSubject` / `createTopic`, decoded by the protocol
 * bindings) and word an empty name or a failure as the capture picker does
 * (`capture/failures.ts`), the backend's Spanish `detail` included.
 */

interface Labels {
  /** The form's accessible name. */
  form: string;
  field: string;
  saving: string;
  empty: string;
  failure: string;
}

function NameForm<T>({
  labels,
  create,
  onCreated,
  autoFocus = false,
}: {
  labels: Labels;
  create: (name: string) => Promise<ApiResult<T>>;
  onCreated: (created: T) => void;
  autoFocus?: boolean;
}) {
  const [name, setName] = useState("");
  const [status, setStatus] = useState<{ state: "idle" } | { state: "saving" } | { state: "failed"; message: string }>(
    { state: "idle" },
  );

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed === "") {
      setStatus({ state: "failed", message: labels.empty });
      return;
    }
    setStatus({ state: "saving" });
    const result = await create(trimmed);
    if (result.kind === "ok") {
      setName("");
      setStatus({ state: "idle" });
      onCreated(result.value);
      return;
    }
    setStatus({ state: "failed", message: describeFailure(labels.failure, result) });
  }

  const saving = status.state === "saving";
  return (
    <form className="desk-create" aria-label={labels.form} onSubmit={submit}>
      <input
        aria-label={labels.field}
        type="text"
        placeholder={labels.field}
        value={name}
        disabled={saving}
        autoFocus={autoFocus}
        onChange={(event) => setName(event.target.value)}
      />
      <button type="submit" disabled={saving}>
        {saving ? labels.saving : "Crear"}
      </button>
      {status.state === "failed" && <p role="alert">{status.message}</p>}
    </form>
  );
}

const SUBJECT_LABELS: Labels = {
  form: "Nueva asignatura",
  field: "Nombre de la asignatura",
  saving: "Creando…",
  empty: "Escribe el nombre de la asignatura.",
  failure: "No se ha podido crear la asignatura",
};

/** "Nueva asignatura": the created subject is handed back so the desk lists it at once. */
export function NewSubject({ onCreated }: { onCreated: (subject: Subject) => void }) {
  return (
    <section className="desk-block desk-new-subject" aria-labelledby="desk-new-subject">
      <h2 id="desk-new-subject">Nueva asignatura</h2>
      <NameForm labels={SUBJECT_LABELS} create={createSubject} onCreated={onCreated} />
    </section>
  );
}

/**
 * "Nuevo tema" under a subject: a toggle that shows the name form; the created topic is handed
 * back (the desk opens it in Construir).
 */
export function NewTopic({ subject, onCreated }: { subject: Subject; onCreated: (topic: Topic) => void }) {
  const [open, setOpen] = useState(false);
  const labels: Labels = {
    form: `Nuevo tema de ${subject.name}`,
    field: "Nombre del tema",
    saving: "Creando…",
    empty: "Escribe el nombre del tema.",
    failure: "No se ha podido crear el tema",
  };
  return (
    <div className="desk-new-topic">
      <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        Nuevo tema
      </button>
      {open && (
        <NameForm
          labels={labels}
          create={(name) => createTopic(subject.subject_id, name)}
          onCreated={onCreated}
          autoFocus
        />
      )}
    </div>
  );
}
