/**
 * Client for `POST /api/subjects/{subject_id}/topics/{topic_id}/sources/pdf` (#149): uploads a
 * PDF, optionally with a page range as the student types it ("82-94", "páginas 82 a 94"), as a
 * source of the topic. The backend imports it and answers what it kept; a refusal carries a
 * Spanish `detail` that is shown as it comes.
 */

/** The backend's 201 answer. */
export interface ImportedPdf {
  source_id: string;
  vault_id: string;
  original_name: string;
  original_page_count: number;
  first_page: number;
  last_page: number;
  page_count: number;
  /** Kept pages, in the original's numbering, with no extractable text (scanned, say). */
  pages_without_text: number[];
}

export type PdfUploadResult =
  | { kind: "ok"; imported: ImportedPdf }
  /** A non-2xx answer with a Spanish `detail` (413 too large, 422 unreadable or bad range...). */
  | { kind: "refused"; status: number; detail: string }
  /** Any other non-2xx status, or a 2xx body that is not an import. */
  | { kind: "error"; status: number }
  /** The request never got an answer (backend down, network error). */
  | { kind: "unreachable" };

export function pdfUploadPath(subjectId: string, topicId: string): string {
  return `/api/subjects/${encodeURIComponent(subjectId)}/topics/${encodeURIComponent(topicId)}/sources/pdf`;
}

function isImportedPdf(body: unknown): body is ImportedPdf {
  if (typeof body !== "object" || body === null) return false;
  const b = body as Record<string, unknown>;
  return (
    typeof b.source_id === "string" &&
    typeof b.vault_id === "string" &&
    typeof b.original_name === "string" &&
    typeof b.original_page_count === "number" &&
    typeof b.first_page === "number" &&
    typeof b.last_page === "number" &&
    typeof b.page_count === "number" &&
    Array.isArray(b.pages_without_text) &&
    b.pages_without_text.every((page) => typeof page === "number")
  );
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

export async function uploadPdf(
  subjectId: string,
  topicId: string,
  file: File,
  pages: string,
): Promise<PdfUploadResult> {
  const form = new FormData();
  form.append("file", file, file.name);
  if (pages.trim() !== "") form.append("pages", pages.trim());
  let response: Response;
  try {
    response = await fetch(pdfUploadPath(subjectId, topicId), { method: "POST", body: form });
  } catch {
    return { kind: "unreachable" };
  }
  const body = await readJson(response);
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string" && detail !== "") {
      return { kind: "refused", status: response.status, detail };
    }
    return { kind: "error", status: response.status };
  }
  if (!isImportedPdf(body)) return { kind: "error", status: response.status };
  return { kind: "ok", imported: body };
}
