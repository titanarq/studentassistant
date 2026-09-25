import { type FormEvent, useEffect, useState } from "react";
import { BOOK_TITLE_MAX, type BookResult, fetchBook, saveBook } from "./api";

/**
 * "Libro de texto" on the topic page (#214): the title of the topic's textbook, so book pages
 * are cited as `Libro «Título», página N`. It shows the stored title (or a placeholder when none
 * is set) and lets the student set or change it. Only a 422's Spanish `detail` (an empty title,
 * or one that looks like a key) is shown as it comes; any other failure gets a generic message,
 * and on a failure the shown title stays the previous one.
 */

const GENERIC_SAVE_ERROR = "No se ha podido guardar el título del libro. Inténtalo de nuevo.";
const GENERIC_LOAD_ERROR = "No se ha podido leer el título del libro.";

type Shown = { state: "loading" } | { state: "loaded"; title: string | null } | { state: "failed" };

type SaveState = { state: "idle" } | { state: "saving" } | { state: "saved" } | { state: "failed"; message: string };

function saveFailure(result: Exclude<BookResult, { kind: "ok" }>): string {
  if (result.kind === "refused" && result.status === 422) return result.detail;
  return GENERIC_SAVE_ERROR;
}

export default function BookTitleForm({ subjectId, topicId }: { subjectId: string; topicId: string }) {
  const [shown, setShown] = useState<Shown>({ state: "loading" });
  const [draft, setDraft] = useState("");
  const [save, setSave] = useState<SaveState>({ state: "idle" });

  useEffect(() => {
    let cancelled = false;
    setShown({ state: "loading" });
    fetchBook(subjectId, topicId).then((result) => {
      if (cancelled) return;
      if (result.kind === "ok") {
        setShown({ state: "loaded", title: result.title });
        setDraft(result.title ?? "");
      } else {
        setShown({ state: "failed" });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSave({ state: "saving" });
    const result = await saveBook(subjectId, topicId, draft);
    if (result.kind === "ok") {
      setShown({ state: "loaded", title: result.title });
      setDraft(result.title ?? "");
      setSave({ state: "saved" });
    } else {
      setSave({ state: "failed", message: saveFailure(result) });
    }
  }

  const saving = save.state === "saving";
  return (
    <form onSubmit={submit} aria-label="Libro de texto" noValidate>
      <h2>Libro de texto</h2>
      <p>
        {shown.state === "loading" && "Cargando el título del libro…"}
        {shown.state === "failed" && GENERIC_LOAD_ERROR}
        {shown.state === "loaded" &&
          (shown.title === null ? "Este tema aún no tiene libro de texto." : `Libro «${shown.title}»`)}
      </p>
      <p>
        <label>
          Título del libro{" "}
          <input
            type="text"
            value={draft}
            maxLength={BOOK_TITLE_MAX}
            disabled={saving}
            onChange={(event) => setDraft(event.target.value)}
          />
        </label>{" "}
        <button type="submit" disabled={saving}>
          {saving ? "Guardando…" : "Guardar"}
        </button>
      </p>
      {save.state === "saved" && <p role="status">Título del libro guardado.</p>}
      {save.state === "failed" && <p role="status">{save.message}</p>}
    </form>
  );
}
