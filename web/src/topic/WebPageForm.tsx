import { type FormEvent, useState } from "react";
import { WEB_PAGE_URL_MAX, WEB_PAGE_URL_PATTERN, type WebPageAddResponse } from "../protocol";
import { addWebPage, describeApiFailure } from "./webSearchApi";

/**
 * "Añadir una página web" on the topic page (#62): the student pastes the address of a page and
 * it is fetched and kept as a web source of the topic, like a kept search result. A page the
 * topic already has is not fetched again; the form says so.
 */

type FormState =
  | { state: "idle" }
  | { state: "saving" }
  | { state: "done"; added: WebPageAddResponse }
  | { state: "failed"; message: string };

export default function WebPageForm({
  subjectId,
  topicId,
  onAdded,
}: {
  subjectId: string;
  topicId: string;
  /** Called after a page was stored, so the page can refresh what it shows of the topic. */
  onAdded?: (added: WebPageAddResponse) => void;
}) {
  const [url, setUrl] = useState("");
  const [status, setStatus] = useState<FormState>({ state: "idle" });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const address = url.trim();
    if (!WEB_PAGE_URL_PATTERN.test(address)) {
      setStatus({ state: "failed", message: "Pega la dirección completa de la página (empieza por http:// o https://)." });
      return;
    }
    setStatus({ state: "saving" });
    const result = await addWebPage(subjectId, topicId, address);
    if (result.kind === "ok") {
      setUrl("");
      setStatus({ state: "done", added: result.value });
      if (!result.value.already_kept) onAdded?.(result.value);
    } else {
      setStatus({ state: "failed", message: `No se ha guardado la página: ${describeApiFailure(result)}` });
    }
  }

  const saving = status.state === "saving";
  return (
    <form onSubmit={submit} aria-label="Añadir una página web" noValidate>
      <h2>Añadir una página web</h2>
      <p>
        <label>
          Dirección de la página{" "}
          <input
            type="url"
            inputMode="url"
            placeholder="https://…"
            value={url}
            maxLength={WEB_PAGE_URL_MAX}
            disabled={saving}
            onChange={(event) => setUrl(event.target.value)}
          />
        </label>{" "}
        <button type="submit" disabled={saving}>
          {saving ? "Descargando…" : "Guardar como fuente"}
        </button>
      </p>
      {status.state === "done" && (
        <p role="status">
          {status.added.already_kept
            ? `Esa página ya era una fuente del tema: «${status.added.title}».`
            : `Página «${status.added.title}» guardada como fuente externa (${status.added.source_id}).`}
        </p>
      )}
      {status.state === "failed" && <p role="status">{status.message}</p>}
    </form>
  );
}
