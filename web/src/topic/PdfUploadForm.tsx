import { type FormEvent, useState } from "react";
import { type ImportedPdf, uploadPdf } from "./api";

/**
 * Adds a PDF, or only a page range of it, to a topic. The range is sent as typed; the backend
 * parses it and its Spanish refusals (too large, unreadable, a range outside the PDF) are shown
 * as they come.
 */

type FormState =
  | { state: "idle" }
  | { state: "uploading" }
  | { state: "done"; imported: ImportedPdf }
  | { state: "failed"; message: string };

function describe(imported: ImportedPdf): string {
  const kept =
    imported.first_page === imported.last_page
      ? `la página ${imported.first_page}`
      : `las páginas ${imported.first_page}-${imported.last_page}`;
  return `PDF «${imported.original_name}» añadido: ${kept} de ${imported.original_page_count}.`;
}

export default function PdfUploadForm({
  subjectId,
  topicId,
}: {
  subjectId: string;
  topicId: string;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [pages, setPages] = useState("");
  const [status, setStatus] = useState<FormState>({ state: "idle" });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (file === null) {
      setStatus({ state: "failed", message: "Elige primero un PDF." });
      return;
    }
    setStatus({ state: "uploading" });
    const result = await uploadPdf(subjectId, topicId, file, pages);
    switch (result.kind) {
      case "ok":
        setStatus({ state: "done", imported: result.imported });
        break;
      case "refused":
        setStatus({ state: "failed", message: `No se ha añadido el PDF: ${result.detail}` });
        break;
      case "error":
        setStatus({
          state: "failed",
          message: `No se ha añadido el PDF: el servidor respondió con un error (${result.status}).`,
        });
        break;
      case "unreachable":
        setStatus({
          state: "failed",
          message: "No se ha añadido el PDF: no se pudo conectar con el servidor.",
        });
        break;
    }
  }

  const uploading = status.state === "uploading";
  return (
    <form onSubmit={submit} aria-label="Añadir un PDF">
      <h2>Añadir un PDF</h2>
      <p>
        <label>
          Archivo PDF{" "}
          <input
            type="file"
            accept="application/pdf,.pdf"
            disabled={uploading}
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </label>
      </p>
      <p>
        <label>
          Páginas (opcional){" "}
          <input
            type="text"
            placeholder="82-94"
            value={pages}
            disabled={uploading}
            onChange={(event) => setPages(event.target.value)}
          />
        </label>
      </p>
      <p>
        <button type="submit" disabled={uploading}>
          {uploading ? "Añadiendo…" : "Añadir PDF"}
        </button>
      </p>
      {status.state === "done" && (
        <div role="status">
          <p>{describe(status.imported)}</p>
          {status.imported.pages_without_text.length > 0 && (
            <p>
              Sin texto extraíble (¿escaneadas?): páginas{" "}
              {status.imported.pages_without_text.join(", ")}.
            </p>
          )}
        </div>
      )}
      {status.state === "failed" && <p role="alert">{status.message}</p>}
    </form>
  );
}
