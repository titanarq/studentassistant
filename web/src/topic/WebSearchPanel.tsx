import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  type ApiResult,
  describeApiFailure,
  fetchWebSearches,
  keepWebResult,
  queueWebSearch,
  type WebSearch,
} from "./webSearchApi";

/**
 * "Busca esto en Internet" on the topic page (#59): a search box, and the topic's searches --
 * the ones asked out loud during a session too -- with the pages Claude offered. Each page can be
 * kept as a web source of the topic ("Guardar como fuente"); a kept one links to the page it
 * copies and says so. While a search is running the list is polled every `pollMs`.
 */

const REQUESTED_BY: Record<string, string> = { voice: "pedida en voz", web: "pedida aquí", editor: "pedida por el editor" };

function statusLine(search: WebSearch): string {
  if (search.status === "queued") return "Buscando…";
  if (search.status === "failed") return `No se pudo buscar: ${search.message || "error desconocido"}`;
  if (search.results.length === 0) return "No se encontró nada útil.";
  return `${search.results.length} ${search.results.length === 1 ? "página" : "páginas"}`;
}

function SearchItem({
  search,
  onKeep,
  keeping,
  errors,
}: {
  search: WebSearch;
  onKeep: (search: WebSearch, index: number) => void;
  keeping: string | null;
  errors: Record<string, string>;
}) {
  const kept = new Map(search.kept.map((k) => [k.index, k]));
  return (
    <li className="web-search">
      <p>
        <strong>«{search.query}»</strong> ({REQUESTED_BY[search.requested_by] ?? search.requested_by}) — {statusLine(search)}
      </p>
      {search.results.length > 0 && (
        <ol className="web-results">
          {search.results.map((result, index) => {
            const key = `${search.search_id}#${index}`;
            const done = kept.get(index);
            return (
              <li key={key} className={result.relevant ? "web-result web-result-relevant" : "web-result"}>
                <a href={result.url} target="_blank" rel="noopener noreferrer">
                  {result.title}
                </a>{" "}
                <span className="web-result-host">{hostOf(result.url)}</span>
                {result.relevant && <span className="web-result-tag"> · recomendada</span>}
                {!result.found_in_search && <span className="web-result-tag"> · sin confirmar en la búsqueda</span>}
                {result.summary && <p>{result.summary}</p>}
                {done ? (
                  <p className="web-result-kept">Guardada como fuente externa ({done.source_id}).</p>
                ) : (
                  <button type="button" disabled={keeping !== null} onClick={() => onKeep(search, index)}>
                    {keeping === key ? "Guardando…" : "Guardar como fuente"}
                  </button>
                )}
                {errors[key] && <p className="web-result-error">{errors[key]}</p>}
              </li>
            );
          })}
        </ol>
      )}
    </li>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return "";
  }
}

export default function WebSearchPanel({
  subjectId,
  topicId,
  onKept,
  pollMs = 2000,
}: {
  subjectId: string;
  topicId: string;
  /** Called after a page was kept, so the page can refresh what it shows of the topic. */
  onKept?: () => void;
  pollMs?: number;
}) {
  const [query, setQuery] = useState("");
  const [searches, setSearches] = useState<WebSearch[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [keeping, setKeeping] = useState<string | null>(null);
  const [keepErrors, setKeepErrors] = useState<Record<string, string>>({});
  const [reload, setReload] = useState(0);

  const refresh = useCallback(() => setReload((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    fetchWebSearches(subjectId, topicId).then((result: ApiResult<WebSearch[]>) => {
      if (cancelled) return;
      if (result.kind === "ok") {
        setSearches(result.value);
        setLoadError(null);
      } else {
        setLoadError(describeApiFailure(result));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, reload]);

  const running = searches?.some((s) => s.status === "queued") ?? false;
  useEffect(() => {
    if (!running) return;
    const timer = setTimeout(refresh, pollMs);
    return () => clearTimeout(timer);
  }, [running, searches, pollMs, refresh]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (query.trim() === "") {
      setSubmitError("Escribe qué quieres buscar.");
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    const result = await queueWebSearch(subjectId, topicId, query.trim());
    setSubmitting(false);
    if (result.kind === "ok") {
      setQuery("");
      refresh();
    } else {
      setSubmitError(`No se ha podido buscar: ${describeApiFailure(result)}`);
    }
  }

  async function keep(search: WebSearch, index: number) {
    const key = `${search.search_id}#${index}`;
    setKeeping(key);
    setKeepErrors((errors) => ({ ...errors, [key]: "" }));
    const result = await keepWebResult(subjectId, topicId, search.search_id, index);
    setKeeping(null);
    if (result.kind === "ok") {
      refresh();
      onKept?.();
    } else {
      setKeepErrors((errors) => ({ ...errors, [key]: `No se ha guardado: ${describeApiFailure(result)}` }));
    }
  }

  return (
    <section aria-labelledby="web-search-heading" className="web-search-panel">
      <h2 id="web-search-heading">Buscar en Internet</h2>
      <form aria-label="Buscar en Internet" onSubmit={submit}>
        <label>
          Qué buscar{" "}
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} maxLength={500} />
        </label>{" "}
        <button type="submit" disabled={submitting}>
          {submitting ? "Buscando…" : "Buscar"}
        </button>
      </form>
      {submitError && <p role="status">{submitError}</p>}
      {loadError && <p>No se pudieron cargar las búsquedas: {loadError}</p>}
      {searches !== null && searches.length > 0 && (
        <ul className="web-searches" aria-label="Búsquedas del tema">
          {searches.map((search) => (
            <SearchItem key={search.search_id} search={search} onKeep={keep} keeping={keeping} errors={keepErrors} />
          ))}
        </ul>
      )}
    </section>
  );
}
