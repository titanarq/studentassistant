import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import { WEB_PAGE_URL_MAX, WEB_PAGE_URL_PATTERN } from "../../protocol";
import { uploadPdf } from "../../topic/api";
import BookTitleForm from "../../topic/BookTitleForm";
import { addWebPage, describeApiFailure } from "../../topic/webSearchApi";

/** Which popover is open above the toolbar, if any. */
type Popover = "none" | "web" | "book" | "upload";

type WebState = { state: "idle" } | { state: "saving" } | { state: "failed"; message: string };

/** An upload run: the file being sent (1-based) of how many, and the refusals so far. */
interface UploadRun {
  current: number;
  total: number;
  failures: string[];
  /** Every file was sent (only a run with refusals stays shown then). */
  done: boolean;
}

function GlobeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18" />
      <path d="M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 15V4" />
      <path d="M7 9l5-5 5 5" />
      <path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4" />
    </svg>
  );
}

function BookIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" />
      <path d="M4 21V5" />
      <path d="M8 7h7" />
    </svg>
  );
}

/**
 * The bottom toolbar of the **Recursos** tab (#461, replacing #384's «Añadir fuente» panel): three
 * icon buttons outside the scrolling list -- **Añadir una página web** (a small popover with the
 * address and **Añadir**), **Subir archivos PDF** (the file picker, several at once, each uploaded
 * whole in turn) and **Libro de texto** (the topic's `BookTitleForm` in the popover). A successful
 * add closes its popover and calls `onAdded`, so the tab re-reads its list; nothing is logged. A
 * refusal stays in the popover in Spanish; Escape closes it and gives the focus back to its button.
 */
export default function AddSourceToolbar({
  subjectId,
  topicId,
  onAdded,
}: {
  subjectId: string;
  topicId: string;
  onAdded: () => void;
}) {
  const [popover, setPopover] = useState<Popover>("none");
  const [url, setUrl] = useState("");
  const [web, setWeb] = useState<WebState>({ state: "idle" });
  const [upload, setUpload] = useState<UploadRun | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const urlInput = useRef<HTMLInputElement | null>(null);
  const buttons = useRef(new Map<Popover, HTMLButtonElement>());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    if (popover === "web") urlInput.current?.focus();
  }, [popover]);

  const close = (focus: boolean) => {
    const was = popover;
    setPopover("none");
    if (was === "upload") setUpload(null);
    if (focus) buttons.current.get(was)?.focus();
  };
  const toggle = (next: Popover) => {
    if (popover === next) {
      close(false);
      return;
    }
    setWeb({ state: "idle" });
    setPopover(next);
  };

  async function submitWeb(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const address = url.trim();
    if (!WEB_PAGE_URL_PATTERN.test(address)) {
      setWeb({ state: "failed", message: "Pega la dirección completa de la página (empieza por http:// o https://)." });
      return;
    }
    setWeb({ state: "saving" });
    const result = await addWebPage(subjectId, topicId, address);
    if (!mounted.current) return;
    if (result.kind !== "ok") {
      setWeb({ state: "failed", message: `No se ha guardado la página: ${describeApiFailure(result)}` });
      return;
    }
    if (result.value.already_kept) {
      setWeb({ state: "failed", message: `Esa página ya era una fuente del tema: «${result.value.title}».` });
      return;
    }
    setUrl("");
    setWeb({ state: "idle" });
    setPopover("none");
    onAdded();
  }

  async function uploadFiles(files: File[]) {
    if (files.length === 0) return;
    setPopover("upload");
    const failures: string[] = [];
    let added = 0;
    for (const [index, file] of files.entries()) {
      setUpload({ current: index + 1, total: files.length, failures: [...failures], done: false });
      const result = await uploadPdf(subjectId, topicId, file, "");
      if (!mounted.current) return;
      if (result.kind === "ok") {
        added++;
        continue;
      }
      const why =
        result.kind === "refused"
          ? result.detail
          : result.kind === "error"
            ? `el servidor respondió con un error (${result.status}).`
            : "no se pudo conectar con el servidor.";
      failures.push(`No se ha añadido «${file.name}»: ${why}`);
    }
    if (added > 0) onAdded();
    if (failures.length === 0) {
      setUpload(null);
      setPopover((open) => (open === "upload" ? "none" : open));
    } else {
      setUpload({ current: files.length, total: files.length, failures, done: true });
      setPopover("upload");
    }
  }

  const uploading = upload !== null && !upload.done;
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" && popover !== "none" && web.state !== "saving" && !uploading) {
      event.preventDefault();
      close(true);
    }
  };
  const register = (key: Popover) => (element: HTMLButtonElement | null) => {
    if (element) buttons.current.set(key, element);
    else buttons.current.delete(key);
  };

  return (
    <div className="resources-toolbar-wrap" onKeyDown={onKeyDown}>
      {popover === "web" && (
        <form className="resources-popover" id="resources-add-web" aria-label="Añadir una página web" onSubmit={submitWeb} noValidate>
          <input
            ref={urlInput}
            type="url"
            inputMode="url"
            aria-label="Dirección de la página"
            placeholder="https://…"
            value={url}
            maxLength={WEB_PAGE_URL_MAX}
            disabled={web.state === "saving"}
            onChange={(event) => setUrl(event.target.value)}
          />
          <button type="submit" disabled={web.state === "saving"}>
            {web.state === "saving" ? "Descargando…" : "Añadir"}
          </button>
          {web.state === "failed" && (
            <p className="resources-popover-error" role="alert">
              {web.message}
            </p>
          )}
        </form>
      )}
      {popover === "book" && (
        <div className="resources-popover resources-popover-book" id="resources-add-book">
          <BookTitleForm
            subjectId={subjectId}
            topicId={topicId}
            onSaved={() => {
              setPopover("none");
              onAdded();
            }}
          />
        </div>
      )}
      {popover === "upload" && upload !== null && (
        <div className="resources-popover" id="resources-add-upload">
          {uploading && (
            <p role="status">
              {upload.total === 1 ? "Subiendo el PDF…" : `Subiendo ${upload.current} de ${upload.total}…`}
            </p>
          )}
          {!uploading &&
            upload.failures.map((failure) => (
              <p key={failure} className="resources-popover-error" role="alert">
                {failure}
              </p>
            ))}
          {!uploading && (
            <button type="button" onClick={() => close(true)}>
              Cerrar
            </button>
          )}
        </div>
      )}
      <div className="resources-toolbar" role="group" aria-label="Añadir fuentes">
        <button
          ref={register("web")}
          type="button"
          className="resources-tool"
          aria-label="Añadir una página web"
          title="Añadir una página web (URL)"
          aria-expanded={popover === "web"}
          aria-controls={popover === "web" ? "resources-add-web" : undefined}
          onClick={() => toggle("web")}
        >
          <GlobeIcon />
        </button>
        <button
          ref={register("upload")}
          type="button"
          className="resources-tool"
          aria-label="Subir archivos PDF"
          title="Subir archivos PDF"
          disabled={uploading}
          onClick={() => fileInput.current?.click()}
        >
          <UploadIcon />
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="application/pdf,.pdf"
          multiple
          hidden
          data-testid="resources-upload-input"
          onChange={(event) => {
            const files = [...(event.target.files ?? [])];
            event.target.value = "";
            void uploadFiles(files);
          }}
        />
        <button
          ref={register("book")}
          type="button"
          className="resources-tool"
          aria-label="Libro de texto"
          title="Título del libro de texto"
          aria-expanded={popover === "book"}
          aria-controls={popover === "book" ? "resources-add-book" : undefined}
          onClick={() => toggle("book")}
        >
          <BookIcon />
        </button>
      </div>
    </div>
  );
}
