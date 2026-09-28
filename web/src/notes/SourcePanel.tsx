import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { describeFailure, type ReadResult } from "../desk/api";
import {
  fetchSourceMeta,
  fetchSourceText,
  fetchTranscript,
  saveTranscription,
  sourceUrl,
  type TranscriptSpan,
} from "./api";
import { originalPage, type Provenance, parseProvenance, sourceVaultId, stemOf } from "./provenance";

/**
 * The sources panel beside the notes: what one provenance footnote cites.
 * - a handwritten or book page: the page image (the flattened `page-NNN.page.jpg` when the capture
 *   produced one, else the cited file), zoomable, and its transcription (the sidecar's
 *   `transcription`, else the derived `page-NNN.md`);
 * - an image pasted into the notes (`sources/images/`, #316): the image, zoomable;
 * - a PDF page: its thumbnail and text (`page-NNN.pKKK.jpg`/`.txt`), numbered as in the original
 *   PDF (`first_page` of the sidecar, `originalPage`);
 * - a web snapshot: its Markdown and the URL it was taken from;
 * - a transcript span: the segments with their timestamps;
 * - `[^ia]`: the AI mark.
 * It is a non-modal dialog: it never moves the notes, Escape or "Cerrar" closes it and the focus
 * goes back to the reference that opened it (the page does that through `onClose`).
 * The `overlay` variant (#473, the workspace's resource detail over the document) closes with an
 * X button (named «Cerrar») and with Escape anywhere on the page, and lets the student correct a
 * page's transcription by hand (`saveTranscription`).
 */

export interface SourcePanelProps {
  subjectId: string;
  topicId: string;
  label: string;
  /** The footnote's definition, `undefined` when the notes cite a label they never define. */
  definition: string | undefined;
  onClose: () => void;
  /** `overlay` (#473): the X close button, Escape anywhere, the editable transcription. */
  variant?: "panel" | "overlay";
}

function useRead<T>(read: () => Promise<ReadResult<T>>, key: string): ReadResult<T> | null {
  const [result, setResult] = useState<{ key: string; value: ReadResult<T> } | null>(null);
  const readRef = useRef(read);
  readRef.current = read;
  useEffect(() => {
    let cancelled = false;
    readRef.current().then((value) => {
      if (!cancelled) setResult({ key, value });
    });
    return () => {
      cancelled = true;
    };
  }, [key]);
  return result !== null && result.key === key ? result.value : null;
}

/** Milliseconds as `MM:SS`, or `H:MM:SS` from one hour on. */
export function formatTimestamp(ms: number): string {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

const ZOOM_STEP = 0.25;
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 4;

function ZoomableImage({ src, fallback, alt }: { src: string; fallback?: string; alt: string }) {
  const [zoom, setZoom] = useState(1);
  const [current, setCurrent] = useState(src);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setCurrent(src);
    setFailed(false);
    setZoom(1);
  }, [src]);
  const change = (delta: number) =>
    setZoom((z) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round((z + delta) * 100) / 100)));
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "+" || event.key === "=") change(ZOOM_STEP);
    else if (event.key === "-") change(-ZOOM_STEP);
    else if (event.key === "0") setZoom(1);
    else return;
    event.preventDefault();
  };
  if (failed) return <p>No se pudo cargar la imagen de la página.</p>;
  return (
    <figure className="source-figure">
      <div className="source-zoom-controls" role="group" aria-label="Zoom de la imagen">
        <button type="button" onClick={() => change(-ZOOM_STEP)} disabled={zoom <= ZOOM_MIN}>
          Alejar
        </button>
        <output aria-live="polite" aria-label="Zoom">
          {Math.round(zoom * 100)} %
        </output>
        <button type="button" onClick={() => change(ZOOM_STEP)} disabled={zoom >= ZOOM_MAX}>
          Acercar
        </button>
        <button type="button" onClick={() => setZoom(1)} disabled={zoom === 1}>
          Tamaño original
        </button>
      </div>
      {/* Focusable, so the keyboard can scroll (pan) and zoom it. */}
      <div
        className="source-image-frame"
        tabIndex={0}
        role="region"
        aria-label="Imagen de la página (usa + y - para el zoom)"
        onKeyDown={onKeyDown}
      >
        <img
          src={current}
          alt={alt}
          style={{ width: `${zoom * 100}%` }}
          onError={() => {
            if (fallback !== undefined && current !== fallback) setCurrent(fallback);
            else setFailed(true);
          }}
        />
      </div>
    </figure>
  );
}

function Failure({ what, result }: { what: string; result: Exclude<ReadResult<unknown>, { kind: "ok" }> }) {
  return (
    <p role="alert">
      {what}: {describeFailure(result)}
    </p>
  );
}

function TextBlock({ result, missing, label }: { result: ReadResult<string> | null; missing: string; label: string }) {
  if (result === null) return <p>Cargando…</p>;
  if (result.kind === "not-found") return <p>{missing}</p>;
  if (result.kind !== "ok") return <Failure what={`No se pudo cargar ${label}`} result={result} />;
  if (result.value.trim() === "") return <p>{missing}</p>;
  return <pre className="source-text">{result.value}</pre>;
}

/**
 * A page's transcription: its `page-NNN.md`, else the sidecar's `transcription` (the order the
 * editor reads them in). With `editable` (#473) the student can correct it by hand.
 */
function PageTranscription({
  vaultId,
  mdResult,
  fromSidecar,
  editable,
}: {
  vaultId: string;
  mdResult: ReadResult<string> | null;
  fromSidecar: string | null;
  editable: boolean;
}) {
  const [saved, setSaved] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement | null>(null);
  const editButton = useRef<HTMLButtonElement | null>(null);
  const refocus = useRef(false);
  const md = mdResult?.kind === "ok" && mdResult.value.trim() !== "" ? mdResult.value : null;
  const sidecar = fromSidecar !== null && fromSidecar.trim() !== "" ? fromSidecar : null;
  const current = saved ?? md ?? sidecar;
  useEffect(() => {
    if (draft !== null) area.current?.focus();
    else if (refocus.current) {
      refocus.current = false;
      editButton.current?.focus();
    }
  }, [draft !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  if (current === null) {
    // Still loading, missing or failed: what a read-only panel shows; nothing to correct yet.
    return <TextBlock result={mdResult} missing="Esta página todavía no está transcrita." label="la transcripción" />;
  }
  const stop = () => {
    refocus.current = true;
    setDraft(null);
    setError(null);
  };
  const save = () => {
    if (draft === null) return;
    if (draft.trim() === "") {
      setError("La transcripción no puede quedar vacía.");
      return;
    }
    setSaving(true);
    setError(null);
    void saveTranscription(vaultId, draft).then((outcome) => {
      setSaving(false);
      if (outcome.kind === "saved") {
        setSaved(outcome.text);
        setNotice("Transcripción guardada.");
        stop();
      } else {
        setError(outcome.message);
      }
    });
  };
  if (draft === null) {
    return (
      <>
        <pre className="source-text">{current}</pre>
        {editable && (
          <div className="source-transcription-actions">
            <button
              ref={editButton}
              type="button"
              onClick={() => {
                setNotice(null);
                setDraft(current);
              }}
            >
              Editar la transcripción
            </button>
            {notice !== null && (
              <p className="source-transcription-notice" role="status">
                {notice}
              </p>
            )}
          </div>
        )}
      </>
    );
  }
  return (
    <form
      className="source-transcription-form"
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <label htmlFor="source-transcription-text">Corrige la transcripción</label>
      <textarea
        id="source-transcription-text"
        ref={area}
        value={draft}
        disabled={saving}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          // Escape leaves the edit (the detail stays open); a second Escape closes it.
          if (event.key === "Escape" && !saving) {
            event.preventDefault();
            event.stopPropagation();
            stop();
          }
        }}
      />
      {error !== null && (
        <p className="source-transcription-error" role="alert">
          No se pudo guardar: {error}
        </p>
      )}
      <div className="source-transcription-actions">
        <button type="submit" className="source-transcription-save" disabled={saving}>
          {saving ? "Guardando…" : "Guardar"}
        </button>
        <button type="button" onClick={stop} disabled={saving}>
          Cancelar
        </button>
      </div>
    </form>
  );
}

function PageSource({
  subjectId,
  topicId,
  source,
  editable,
}: {
  subjectId: string;
  topicId: string;
  source: Extract<Provenance, { kind: "page" }>;
  editable: boolean;
}) {
  const vaultId = sourceVaultId(subjectId, topicId, source.sourceKind, source.file);
  const stem = stemOf(source.file);
  const derived = (suffix: string) => sourceVaultId(subjectId, topicId, source.sourceKind, `${stem}.${suffix}`);
  const meta = useRead(() => fetchSourceMeta(vaultId), vaultId);
  const transcription = useRead(() => fetchSourceText(derived("md")), `${vaultId}#md`);
  // The sidecar only matters for its transcription; the image stands on its own.
  const fromSidecar = meta?.kind === "ok" ? meta.value.transcription : null;
  return (
    <>
      <ZoomableImage src={sourceUrl(derived("page.jpg"))} fallback={sourceUrl(vaultId)} alt={source.text} />
      <h3>Transcripción</h3>
      <PageTranscription vaultId={vaultId} mdResult={transcription} fromSidecar={fromSidecar} editable={editable} />
    </>
  );
}

function ImageSource({ subjectId, topicId, source }: { subjectId: string; topicId: string; source: Extract<Provenance, { kind: "image" }> }) {
  return <ZoomableImage src={sourceUrl(sourceVaultId(subjectId, topicId, "images", source.file))} alt={source.text} />;
}

function PdfSource({ subjectId, topicId, source }: { subjectId: string; topicId: string; source: Extract<Provenance, { kind: "pdf" }> }) {
  const vaultId = sourceVaultId(subjectId, topicId, "pdf", source.file);
  const stem = stemOf(source.file);
  const page = source.page ?? 1;
  const suffix = `p${String(page).padStart(3, "0")}`;
  const derived = (ext: string) => sourceVaultId(subjectId, topicId, "pdf", `${stem}.${suffix}.${ext}`);
  const meta = useRead(() => fetchSourceMeta(vaultId), vaultId);
  const text = useRead(() => fetchSourceText(derived("txt")), `${vaultId}#${suffix}`);
  if (meta === null) return <p>Cargando…</p>;
  if (meta.kind !== "ok") return <Failure what="No se pudo cargar el PDF" result={meta} />;
  const sidecar = meta.value.meta;
  const name = typeof sidecar?.original_name === "string" ? sidecar.original_name : source.file;
  return (
    <>
      <p className="source-caption">
        {source.page === null ? `PDF «${name}»` : `PDF «${name}», página ${originalPage(sidecar, page)}`}
      </p>
      <ZoomableImage src={sourceUrl(derived("jpg"))} alt={source.text} />
      <p>
        <a href={`${sourceUrl(vaultId)}#page=${page}`} target="_blank" rel="noopener noreferrer">
          Abrir el PDF
        </a>
      </p>
      <h3>Texto de la página</h3>
      <TextBlock result={text} missing="Esta página no tiene texto extraído (puede ser escaneada)." label="el texto" />
    </>
  );
}

function WebSource({ subjectId, topicId, source }: { subjectId: string; topicId: string; source: Extract<Provenance, { kind: "web" }> }) {
  const vaultId = sourceVaultId(subjectId, topicId, "web", source.file);
  const meta = useRead(() => fetchSourceMeta(vaultId), `${vaultId}#meta`);
  const text = useRead(() => fetchSourceText(vaultId), vaultId);
  const sidecar: Record<string, unknown> | null = meta?.kind === "ok" ? meta.value.meta : null;
  const url = typeof sidecar?.url === "string" && /^https?:\/\//i.test(sidecar.url) ? sidecar.url : null;
  const fetched = typeof sidecar?.fetched_at === "string" ? sidecar.fetched_at : null;
  return (
    <>
      {url !== null && (
        <p className="source-caption">
          Fuente externa: copia de{" "}
          <a href={url} target="_blank" rel="noopener noreferrer">
            {url}
          </a>
          {fetched !== null && ` (${fetched})`}
        </p>
      )}
      <TextBlock result={text} missing="La copia de la web está vacía." label="la copia de la web" />
    </>
  );
}

function TranscriptSource({ subjectId, topicId, source }: { subjectId: string; topicId: string; source: Extract<Provenance, { kind: "transcript" }> }) {
  const span = useRead<TranscriptSpan>(
    () => fetchTranscript(subjectId, topicId, source.sessionId, source.span),
    `${source.sessionId}#${source.span}`,
  );
  if (span === null) return <p>Cargando…</p>;
  if (span.kind !== "ok") return <Failure what="No se pudo cargar la transcripción" result={span} />;
  if (span.value.segments.length === 0) return <p>No se dijo nada en ese tramo.</p>;
  return (
    <ol className="source-transcript" aria-label="Fragmento de la transcripción">
      {span.value.segments.map((segment) => (
        <li key={segment.seq}>
          <time>{formatTimestamp(segment.t_start)}</time> {segment.text}
        </li>
      ))}
    </ol>
  );
}

function CloseIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

export default function SourcePanel({ subjectId, topicId, label, definition, onClose, variant = "panel" }: SourcePanelProps) {
  const overlay = variant === "overlay";
  const headingRef = useRef<HTMLHeadingElement>(null);
  const provenance: Provenance | null = definition === undefined ? null : parseProvenance(label, definition);

  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [label]);

  const onKeyDown = (event: KeyboardEvent) => {
    if (!overlay && event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  // The overlay closes with Escape wherever the focus is, unless something else took the key
  // first (a card's delete confirmation, the transcription editor).
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!overlay) return;
    const onWindowKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      closeRef.current();
    };
    window.addEventListener("keydown", onWindowKey);
    return () => window.removeEventListener("keydown", onWindowKey);
  }, [overlay]);

  const title = provenance === null ? `Fuente ${label}` : provenance.kind === "ia" ? "Ampliado por la IA" : provenance.text;

  return (
    <aside className={overlay ? "source-panel source-panel-overlay" : "source-panel"} role="dialog" aria-modal="false" aria-labelledby="source-panel-title" onKeyDown={onKeyDown}>
      <header className="source-panel-header">
        <h2 id="source-panel-title" tabIndex={-1} ref={headingRef}>
          {title}
        </h2>
        {overlay ? (
          <button type="button" className="source-panel-close" aria-label="Cerrar" title="Cerrar (Esc)" onClick={onClose}>
            <CloseIcon />
          </button>
        ) : (
          <button type="button" onClick={onClose}>
            Cerrar
          </button>
        )}
      </header>
      <div className="source-panel-body">
        {provenance === null && <p>Los apuntes citan esta fuente pero no dicen cuál es.</p>}
        {provenance?.kind === "ia" && (
          <p className="notes-ia">{provenance.text}. Revísalo antes de estudiarlo como tuyo.</p>
        )}
        {provenance?.kind === "unknown" && <p>{provenance.text}</p>}
        {provenance?.kind === "page" && <PageSource key={label} subjectId={subjectId} topicId={topicId} source={provenance} editable={overlay} />}
        {provenance?.kind === "image" && <ImageSource key={label} subjectId={subjectId} topicId={topicId} source={provenance} />}
        {provenance?.kind === "pdf" && <PdfSource key={label} subjectId={subjectId} topicId={topicId} source={provenance} />}
        {provenance?.kind === "web" && <WebSource key={label} subjectId={subjectId} topicId={topicId} source={provenance} />}
        {provenance?.kind === "transcript" && (
          <TranscriptSource key={label} subjectId={subjectId} topicId={topicId} source={provenance} />
        )}
      </div>
    </aside>
  );
}
