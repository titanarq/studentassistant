import {
  type ClipboardEvent,
  type DragEvent,
  forwardRef,
  type ReactNode,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import type { BlockKind, VisualEditor } from "./visualEditor";
import "./noteEditor.css";

/**
 * The notes editor (#316): **Visual** (Milkdown, `visualEditor.ts`, which saves untouched blocks
 * byte for byte and shows provenance references as fixed chips) or **Markdown** (the raw text with
 * a live preview). Both have a toolbar with **Tabla** and **Imagen**; pasting or dropping an image
 * uploads it (`uploadImage`) and inserts the Markdown the server answers at the cursor, with a
 * placeholder while it uploads and a Spanish error (nothing inserted) when it fails.
 *
 * The text is read with the handle's `getText()` (the parent saves it); `leading` and `actions`
 * are laid out in the editor's header around the Visual | Markdown switch.
 */

export type EditorMode = "visual" | "markdown";

export interface NoteEditorHandle {
  /** The document as it is now, in either mode. */
  getText(): string;
}

export type UploadResult = { kind: "ok"; markdown: string } | { kind: "failed"; message: string };

export interface NoteEditorProps {
  /** The text the editor starts from (read once, at mount). */
  initialText: string;
  initialMode?: EditorMode;
  /** Stores a pasted or chosen image and answers the Markdown that shows it. */
  uploadImage: (file: File) => Promise<UploadResult>;
  /** The URL an image of the notes is shown from in the visual editor. */
  resolveImage?: (src: string) => string;
  /** The live preview of the Markdown mode. */
  renderPreview: (text: string) => ReactNode;
  /** Called on every change of the text. */
  onChange?: () => void;
  leading?: ReactNode;
  actions?: ReactNode;
}

export const TABLE_SKELETON = "| Columna 1 | Columna 2 |\n| --- | --- |\n|  |  |\n";
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];
export const UPLOADING = "Subiendo imagen pegada…";
export const NOT_AN_IMAGE = "Solo se pueden pegar imágenes PNG, JPEG o WebP.";

function imageOf(files: FileList | null | undefined, items?: DataTransferItemList | null): File | null {
  for (const file of Array.from(files ?? [])) if (file.type.startsWith("image/")) return file;
  for (const item of Array.from(items ?? [])) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) return file;
    }
  }
  return null;
}

const NoteEditor = forwardRef<NoteEditorHandle, NoteEditorProps>(function NoteEditor(
  { initialText, initialMode = "visual", uploadImage, resolveImage, renderPreview, onChange, leading, actions },
  ref,
) {
  const [mode, setMode] = useState<EditorMode>(initialMode);
  const [markdown, setMarkdown] = useState(initialText);
  const [ready, setReady] = useState(false);
  const [uploads, setUploads] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [visualError, setVisualError] = useState<string | null>(null);
  const text = useRef(initialText);
  const visual = useRef<VisualEditor | null>(null);
  const surface = useRef<HTMLDivElement | null>(null);
  const textarea = useRef<HTMLTextAreaElement | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const mounted = useRef(true);
  const changed = useRef(onChange);
  changed.current = onChange;
  const resolver = useRef(resolveImage);
  resolver.current = resolveImage;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const current = useCallback((): string => {
    if (mode === "visual" && visual.current !== null) return visual.current.getMarkdown();
    return text.current;
  }, [mode]);

  useImperativeHandle(ref, () => ({ getText: current }), [current]);

  // The visual editor lives while the mode is visual; leaving it keeps its text.
  useEffect(() => {
    if (mode !== "visual" || surface.current === null) return;
    const root = surface.current;
    let cancelled = false;
    let editor: VisualEditor | null = null;
    setReady(false);
    setVisualError(null);
    // Milkdown is loaded only when the student edits (it is about half of the app's size).
    import("./visualEditor")
      .then(({ createVisualEditor }) =>
        createVisualEditor(root, text.current, {
          onChange: () => changed.current?.(),
          resolveImage: (src) => resolver.current?.(src) ?? src,
        }),
      )
      .then(
      (created) => {
        if (cancelled) {
          void created.destroy();
          return;
        }
        editor = created;
        visual.current = created;
        setReady(true);
      },
      () => {
        if (!cancelled) setVisualError("No se pudo abrir el editor visual; usa el modo Markdown.");
      },
    );
    return () => {
      cancelled = true;
      if (editor !== null) {
        text.current = editor.getMarkdown();
        void editor.destroy();
      }
      visual.current = null;
      root.replaceChildren();
    };
  }, [mode]);

  const switchTo = (next: EditorMode) => {
    if (next === mode) return;
    if (mode === "visual" && visual.current !== null) text.current = visual.current.getMarkdown();
    setMarkdown(text.current);
    setMode(next);
  };

  const editMarkdown = (value: string) => {
    text.current = value;
    setMarkdown(value);
    onChange?.();
  };

  /** Inserts `snippet` as its own block at the textarea's cursor. */
  const insertInTextarea = (snippet: string, at: number | null = null) => {
    const value = text.current;
    const area = textarea.current;
    const position = at ?? area?.selectionStart ?? value.length;
    const before = value.slice(0, position);
    const after = value.slice(position);
    const lead = before === "" || before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
    const block = snippet.endsWith("\n") ? snippet : `${snippet}\n`;
    const trail = after === "" || after.startsWith("\n") ? "" : "\n";
    editMarkdown(`${before}${lead}${block}${trail}${after}`);
  };

  const upload = async (file: File) => {
    setUploadError(null);
    if (!IMAGE_TYPES.includes(file.type)) {
      setUploadError(NOT_AN_IMAGE);
      return;
    }
    const at = mode === "markdown" ? (textarea.current?.selectionStart ?? null) : null;
    setUploads((n) => n + 1);
    const result = await uploadImage(file);
    if (!mounted.current) return;
    setUploads((n) => n - 1);
    if (result.kind === "failed") {
      setUploadError(`No se pudo subir la imagen: ${result.message}`);
      return;
    }
    if (visual.current !== null) {
      visual.current.insertMarkdown(result.markdown);
    } else {
      insertInTextarea(result.markdown, at);
    }
  };

  const onPaste = (event: ClipboardEvent) => {
    const file = imageOf(event.clipboardData?.files, event.clipboardData?.items);
    if (file === null) return;
    event.preventDefault();
    event.stopPropagation();
    void upload(file);
  };

  const onDrop = (event: DragEvent) => {
    const file = imageOf(event.dataTransfer?.files, event.dataTransfer?.items);
    if (file === null) return;
    event.preventDefault();
    event.stopPropagation();
    void upload(file);
  };

  const onDragOver = (event: DragEvent) => {
    if (Array.from(event.dataTransfer?.types ?? []).includes("Files")) event.preventDefault();
  };

  const withVisual = (action: (editor: VisualEditor) => void) => () => {
    if (visual.current === null) return;
    action(visual.current);
    visual.current.focus();
  };

  const visualTools = mode === "visual";
  return (
    <div className="note-editor" data-mode={mode}>
      <div className="note-editor-header">
        {leading}
        <div className="note-editor-spacer" />
        <div className="note-editor-modes" role="group" aria-label="Modo del editor">
          <button type="button" aria-pressed={mode === "visual"} onClick={() => switchTo("visual")}>
            Visual
          </button>
          <button type="button" aria-pressed={mode === "markdown"} onClick={() => switchTo("markdown")}>
            Markdown
          </button>
        </div>
        {actions}
      </div>
      <div className="note-editor-frame" onPasteCapture={onPaste} onDropCapture={onDrop} onDragOver={onDragOver}>
        <div className="note-editor-toolbar" role="toolbar" aria-label="Formato">
          {visualTools && (
            <>
              <label className="note-editor-sr" htmlFor="note-editor-block">
                Tipo de bloque
              </label>
              <select
                id="note-editor-block"
                defaultValue=""
                disabled={!ready}
                onChange={(event) => {
                  const kind = event.target.value as BlockKind | "";
                  if (kind !== "") withVisual((editor) => editor.setBlock(kind))();
                  event.target.value = "";
                }}
              >
                <option value="" disabled>
                  Bloque…
                </option>
                <option value="paragraph">Párrafo</option>
                <option value="h2">Título 2</option>
                <option value="h3">Título 3</option>
              </select>
              <span className="note-editor-separator" aria-hidden="true" />
              <button type="button" className="note-tool note-tool-strong" aria-label="Negrita" disabled={!ready} onClick={withVisual((e) => e.toggleStrong())}>
                B
              </button>
              <button type="button" className="note-tool note-tool-em" aria-label="Cursiva" disabled={!ready} onClick={withVisual((e) => e.toggleEmphasis())}>
                I
              </button>
              <button type="button" className="note-tool" aria-label="Lista" disabled={!ready} onClick={withVisual((e) => e.bulletList())}>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                  <path d="M9 6h11M9 12h11M9 18h11" />
                  <circle cx="4.5" cy="6" r="1" />
                  <circle cx="4.5" cy="12" r="1" />
                  <circle cx="4.5" cy="18" r="1" />
                </svg>
              </button>
              <span className="note-editor-separator" aria-hidden="true" />
            </>
          )}
          <button
            type="button"
            className="note-tool note-tool-labelled"
            disabled={visualTools && !ready}
            onClick={visualTools ? withVisual((e) => e.insertTable()) : () => insertInTextarea(TABLE_SKELETON)}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <path d="M3 10h18M3 15h18M10 4v16" />
            </svg>
            Tabla
          </button>
          {visualTools && (
            <>
              <button type="button" className="note-tool" aria-label="Añadir fila" title="Añadir fila" disabled={!ready} onClick={withVisual((e) => e.addRow())}>
                +fila
              </button>
              <button type="button" className="note-tool" aria-label="Añadir columna" title="Añadir columna" disabled={!ready} onClick={withVisual((e) => e.addColumn())}>
                +col
              </button>
            </>
          )}
          <button type="button" className="note-tool note-tool-labelled" onClick={() => fileInput.current?.click()}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <circle cx="9" cy="10" r="2" />
              <path d="M21 17l-5-5-9 8" />
            </svg>
            Imagen
          </button>
          <input
            ref={fileInput}
            className="note-editor-sr"
            type="file"
            accept={IMAGE_TYPES.join(",")}
            tabIndex={-1}
            aria-label="Elegir una imagen"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void upload(file);
            }}
          />
          <div className="note-editor-spacer" />
          <span className="note-editor-hint">Pega una imagen con Ctrl+V</span>
        </div>
        {uploads > 0 && (
          <div className="note-editor-upload" role="status">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <circle cx="9" cy="10" r="2" />
              <path d="M21 17l-5-5-9 8" />
            </svg>
            <div>
              <p className="note-editor-upload-title">{UPLOADING}</p>
              <div className="note-editor-progress" aria-hidden="true" />
              <p className="note-editor-upload-note">Se guardará como fuente del tema</p>
            </div>
          </div>
        )}
        {uploadError !== null && (
          <p className="note-editor-error" role="alert">
            {uploadError}
          </p>
        )}
        {visualError !== null && (
          <p className="note-editor-error" role="alert">
            {visualError}
          </p>
        )}
        {mode === "visual" ? (
          <div key="visual" ref={surface} className="note-editor-visual notes-body" aria-label="Apuntes en edición" aria-busy={!ready} />
        ) : (
          <div key="markdown" className="note-editor-split">
            <label className="note-editor-sr" htmlFor="note-editor-markdown">
              Apuntes en Markdown
            </label>
            <textarea
              id="note-editor-markdown"
              ref={textarea}
              className="note-editor-markdown"
              value={markdown}
              spellCheck
              onChange={(event) => editMarkdown(event.target.value)}
            />
            <div className="note-editor-preview" aria-label="Vista previa">
              {renderPreview(markdown)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
});

export default NoteEditor;
