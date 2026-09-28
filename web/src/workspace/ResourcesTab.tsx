import { type KeyboardEvent, type MouseEvent, useEffect, useMemo, useRef, useState } from "react";
import { describeFailure, fetchTopicSummary, type ReadResult, type TopicSummary } from "../desk/api";
import type { NotesTree } from "../notes/markdown";
import { sourceUrl } from "../notes/api";
import AddSourceToolbar from "./resources/AddSourceToolbar";
import { DELETE_UNSUPPORTED, deleteSource, fetchTopicSources, GROUP_TITLES, resourceList, type TopicSources } from "./resources";
import { resourceStates, type SourceEntry, sourceRefOf, thumbnailOf } from "./resources/state";
import { sourceVaultId } from "../notes/provenance";
import { chipTitle, type SelectedSource, type SourceSelection, topicSourceId, useSelection } from "./resources/selection";
import { useSourceMetas } from "./resources/useSourceMetas";

/** The source whose detail is open (over the document, #473): a footnote label and its definition. */
export interface OpenResource {
  label: string;
  definition: string | undefined;
}

export interface ResourcesTabProps {
  subjectId: string;
  topicId: string;
  tree: NotesTree | null;
  /** Bumped each time the tab is shown, so the counts are read again. */
  refreshKey: number;
  /** Opens a source's detail (over the document, #473); `trigger` gets the focus back on close. */
  onOpen: (resource: OpenResource, trigger: HTMLElement) => void;
}

/** What the tab read: the topic's source list, or the summary when the backend has no list. */
type Loaded = { kind: "listed"; value: TopicSources } | ReadResult<TopicSummary>;

/** `GET .../sources`, falling back to `GET .../summary` when it does not answer a list (#323). */
async function loadSources(subjectId: string, topicId: string): Promise<Loaded> {
  const listed = await fetchTopicSources(subjectId, topicId);
  if (listed.kind === "ok") return { kind: "listed", value: listed.value };
  return fetchTopicSummary(subjectId, topicId);
}

/** The corner badge of a source the current notes cite (#461). */
export const INCORPORATED_LABEL = "Incorporada a los apuntes";

/** «1 seleccionada», «3 seleccionadas». */
export function selectedText(n: number): string {
  return n === 1 ? "1 seleccionada" : `${n} seleccionadas`;
}

/** The selection's view of a source card. */
function selectedOf(entry: SourceEntry): SelectedSource {
  return { id: topicSourceId(entry.ref), title: chipTitle(entry.ref, entry.title) };
}

function Thumbnail({ entry }: { entry: SourceEntry }) {
  const thumb = thumbnailOf(entry.vaultId, entry.ref);
  const [src, setSrc] = useState(thumb?.src ?? null);
  useEffect(() => setSrc(thumb?.src ?? null), [thumb?.src]);
  if (thumb === null || src === null) {
    return (
      <span className="resource-thumb resource-thumb-empty" aria-hidden="true">
        {entry.ref.kind === "web" ? "Web" : ""}
      </span>
    );
  }
  return (
    <img
      className="resource-thumb"
      src={sourceUrl(src)}
      alt=""
      loading="lazy"
      onError={() => setSrc(src === thumb.fallback || thumb.fallback === null ? null : thumb.fallback)}
    />
  );
}

interface CardSelection {
  selected: boolean;
  onToggle: (shift: boolean) => void;
}

/** The trash button's glyph (an outline bin), hidden from assistive technology. */
function TrashIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M6 6l1 14h10l1-14" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  );
}

/** Where a card's delete is: idle, asking (the inline confirmation), running, or refused. */
type Deletion = { kind: "idle" } | { kind: "asking" } | { kind: "deleting" } | { kind: "failed"; message: string };

/**
 * The card's trash button and its inline confirmation (#450): «¿Borrar esta fuente?» with
 * **Borrar** and **Cancelar** over the thumbnail, never a browser dialog. Escape cancels; the
 * focus goes to **Cancelar** when asking and back to the trash button after cancelling.
 */
function DeleteControl({ entry, onDeleted }: { entry: SourceEntry; onDeleted: (vaultId: string) => void }) {
  const [deletion, setDeletion] = useState<Deletion>({ kind: "idle" });
  const trash = useRef<HTMLButtonElement | null>(null);
  const cancel = useRef<HTMLButtonElement | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const asking = deletion.kind !== "idle";
  // The trash button is back only after the render that closes the confirmation.
  const refocus = useRef(false);
  useEffect(() => {
    if (deletion.kind === "asking" || deletion.kind === "failed") cancel.current?.focus();
    if (deletion.kind === "idle" && refocus.current) {
      refocus.current = false;
      trash.current?.focus();
    }
  }, [deletion.kind]);

  const close = () => {
    refocus.current = true;
    setDeletion({ kind: "idle" });
  };
  const confirm = () => {
    setDeletion({ kind: "deleting" });
    void deleteSource(entry.vaultId).then((outcome) => {
      if (!mounted.current) return;
      if (outcome.kind === "ok") {
        setDeletion({ kind: "idle" });
        onDeleted(entry.vaultId);
        return;
      }
      setDeletion({ kind: "failed", message: outcome.kind === "unsupported" ? DELETE_UNSUPPORTED : outcome.message });
    });
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" && deletion.kind !== "deleting") {
      event.preventDefault();
      close();
    }
  };

  return (
    <>
      {!asking && (
        <button
          ref={trash}
          type="button"
          className="resource-delete"
          aria-label={`Borrar ${entry.title}`}
          title="Borrar esta fuente"
          onClick={() => setDeletion({ kind: "asking" })}
        >
          <TrashIcon />
        </button>
      )}
      {asking && (
        <div className="resource-confirm" role="group" aria-label={`Borrar ${entry.title}`} onKeyDown={onKeyDown}>
          {deletion.kind === "failed" ? (
            <p className="resource-delete-failed" role="alert">
              No se pudo borrar: {deletion.message}
            </p>
          ) : (
            <p>{deletion.kind === "deleting" ? "Borrando…" : "¿Borrar esta fuente?"}</p>
          )}
          <div className="resource-confirm-actions">
            {deletion.kind !== "failed" && (
              <button type="button" className="resource-confirm-yes" onClick={confirm} disabled={deletion.kind === "deleting"}>
                Borrar
              </button>
            )}
            <button ref={cancel} type="button" onClick={close} disabled={deletion.kind === "deleting"}>
              {deletion.kind === "failed" ? "Cerrar" : "Cancelar"}
            </button>
          </div>
        </div>
      )}
    </>
  );
}

function IncorporatedIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </svg>
  );
}

/**
 * One source: the thumbnail with the selection checkbox (top left), the trash button (top right)
 * and, in its bottom corners, a small badge when the notes already cite it (bottom right,
 * «Incorporada a los apuntes») or when the triage flagged it (bottom left, «Aviso: …»); below, its
 * short title and, for a set-aside source, why. No state chip (#461): a source without a badge is
 * simply not in the notes yet.
 */
function SourceCard({
  entry,
  onOpen,
  selection,
  onDeleted,
}: {
  entry: SourceEntry;
  onOpen: (resource: OpenResource, trigger: HTMLElement) => void;
  selection: CardSelection | null;
  onDeleted: (vaultId: string) => void;
}) {
  const reasons = entry.reasons.join(", ");
  const detail = entry.state === "set_aside" ? [reasons, entry.byStudent ? "(la apartaste tú)" : ""].filter((part) => part !== "").join(" ") : "";
  const selected = selection?.selected === true;
  const warning = entry.flagged && reasons !== "" ? `Aviso: ${reasons}` : null;
  return (
    <li className={`resource-card resource-${entry.state.replace("_", "-")}${selected ? " resource-selected" : ""}`}>
      {selection !== null && (
        <label className="resource-check">
          <input
            type="checkbox"
            aria-label={`Seleccionar ${entry.title}`}
            checked={selected}
            // A click (or Space, which the browser turns into a click) toggles; Shift adds the range.
            onClick={(event: MouseEvent<HTMLInputElement>) => selection.onToggle(event.shiftKey)}
            onChange={() => undefined}
          />
          <span className="resource-check-mark" aria-hidden="true">
            ✓
          </span>
        </label>
      )}
      <DeleteControl entry={entry} onDeleted={onDeleted} />
      <button
        type="button"
        className="resource-open"
        onClick={(event) => onOpen({ label: entry.label, definition: entry.definition }, event.currentTarget)}
      >
        <span className="resource-thumb-box">
          <Thumbnail entry={entry} />
          {entry.state === "incorporated" && (
            <span className="resource-badge resource-badge-in" role="img" aria-label={INCORPORATED_LABEL} title={INCORPORATED_LABEL}>
              <IncorporatedIcon />
            </span>
          )}
          {warning !== null && (
            <span className="resource-badge resource-badge-warn" role="img" aria-label={warning} title={warning}>
              !
            </span>
          )}
        </span>
        <span className="resource-title">{entry.title}</span>
        {detail !== "" && <span className="resource-reason">{detail}</span>}
      </button>
    </li>
  );
}

/** The card's part of the selection, or null when the tab is outside a workspace page. */
function cardSelection(selection: SourceSelection | null, entry: SourceEntry, visible: readonly SelectedSource[]): CardSelection | null {
  if (selection === null) return null;
  const source = selectedOf(entry);
  return {
    selected: selection.selected.some((s) => s.id === source.id),
    onToggle: (shift) => selection.toggle(source, visible, shift),
  };
}

/**
 * The **Recursos** tab (#312, #328, #323, #461): every source of the topic (`GET .../sources`, or
 * the summary's counts on an older backend) as a thumbnail -- the ones the current notes cite with
 * a small corner badge, set-aside ones (by the capture triage or by the student, with the reason)
 * in a collapsed "Apartadas (N)" group after the kept ones. Below the scrolling list, a bottom
 * icon toolbar (`AddSourceToolbar`, #461) adds a web page, PDFs or the textbook's title and then
 * the list is read again. A retired source (#451) is not listed, even when the notes cite it
 * (#456). The only per-source action is the
 * trash button over each thumbnail (#450, `DELETE /api/sources/{id}` after an inline
 * confirmation); incorporating, setting aside and restoring are asked in the chat. Each stored source has a checkbox (#432): the ticked ones are the
 * workspace's selection (`resources/selection.ts`), shown as chips above the chat input and sent
 * with the next message; Shift-click ticks a range, «Quitar selección» clears it. Choosing a source hands it to the page (`onOpen`), which since #473
 * shows its detail over the document column; the list stays as it was. The
 * sources' metadata is read again each time the tab is shown and after each change of the notes.
 */
export default function ResourcesTab({ subjectId, topicId, tree, refreshKey, onOpen }: ResourcesTabProps) {
  const [summary, setSummary] = useState<Loaded | null>(null);
  const [showSetAside, setShowSetAside] = useState(false);
  // Bumped after the toolbar added something: the list is read again, as `refreshKey` does.
  const [added, setAdded] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void loadSources(subjectId, topicId).then((result) => {
      if (!cancelled) setSummary(result);
    });
    return () => {
      cancelled = true;
    };
  }, [subjectId, topicId, refreshKey, added]);

  const sources =
    summary?.kind === "listed" ? summary.value.sources : summary?.kind === "ok" ? summary.value.sources : null;
  const list = useMemo(() => resourceList(sources, tree), [sources, tree]);
  const items = useMemo(() => list.groups.flatMap((group) => group.items), [list]);
  // A new object whenever the tab is shown or the notes change: every source is read again.
  const reloadKey = useMemo(() => ({ refreshKey, added, tree }), [refreshKey, added, tree]);
  // Every stored source listed or cited, the ones only the notes cite too (#456: they wait for it).
  const vaultIds = useMemo(
    () => [
      ...new Set(
        items.flatMap((item) => {
          const ref = sourceRefOf(item.label, item.definition);
          return ref === null ? [] : [sourceVaultId(subjectId, topicId, ref.kind, ref.file)];
        }),
      ),
    ],
    [subjectId, topicId, items],
  );
  const metas = useSourceMetas(vaultIds, reloadKey);
  // Sources retired from this tab (#451): gone at once, before the list and their metadata are read again.
  const [retired, setRetired] = useState<ReadonlySet<string>>(new Set());
  const states = useMemo(() => {
    const all = resourceStates(subjectId, topicId, items, tree, metas);
    if (retired.size === 0) return all;
    const keep = (entry: SourceEntry) => !retired.has(entry.vaultId);
    return { ...all, kept: all.kept.filter(keep), setAside: all.setAside.filter(keep) };
  }, [subjectId, topicId, items, tree, metas, retired]);
  const selection = useSelection();
  // The visible order of the cards (kept, then set aside), for a shift-click range.
  const visible = useMemo(() => [...states.kept, ...(showSetAside ? states.setAside : [])].map(selectedOf), [states, showSetAside]);
  const listed = summary?.kind === "listed" || (summary?.kind === "ok" && tree !== null);
  const prune = selection?.prune;
  useEffect(() => {
    if (!listed || prune === undefined) return;
    prune(new Map([...states.kept, ...states.setAside].map((entry) => [topicSourceId(entry.ref), chipTitle(entry.ref, entry.title)])));
  }, [listed, prune, states]);

  const total = states.kept.length + states.setAside.length;
  // A deleted source (#450) leaves the list when it is read again; the selection drops it then.
  const onDeleted = (vaultId: string) => {
    setRetired((current) => new Set(current).add(vaultId));
    setAdded((count) => count + 1);
  };
  const setAsideId = "workspace-resources-set-aside";

  return (
    <div className="workspace-resources">
      <div className="workspace-resources-list">
        {summary === null && <p>Cargando las fuentes…</p>}
        {summary !== null && summary.kind !== "ok" && summary.kind !== "listed" && (
          <p role="alert">No se pudieron cargar las fuentes del tema: {describeFailure(summary)}</p>
        )}
        {summary !== null && total === 0 && states.others.length === 0 && (
          <p className="workspace-resources-empty">Este tema todavía no tiene fuentes: captura páginas en la pestaña Captura o añádelas abajo.</p>
        )}
        {selection !== null && selection.selected.length > 0 && (
          <div className="resources-selection" role="group" aria-label="Selección">
            <p aria-live="polite">{selectedText(selection.selected.length)}</p>
            <button type="button" onClick={selection.clear}>
              Quitar selección
            </button>
          </div>
        )}
        {states.kept.length > 0 && (
          <ul className="resource-grid" aria-label="Fuentes del tema">
            {states.kept.map((entry) => (
              <SourceCard
                key={entry.key}
                entry={entry}
                onOpen={onOpen}
                selection={cardSelection(selection, entry, visible)}
                onDeleted={onDeleted}
              />
            ))}
          </ul>
        )}
        {states.setAside.length > 0 && (
          <section className="resource-set-aside">
            <h3>
              <button
                type="button"
                className="resource-disclosure"
                aria-expanded={showSetAside}
                aria-controls={setAsideId}
                onClick={() => setShowSetAside((shown) => !shown)}
              >
                Apartadas ({states.setAside.length})
              </button>
            </h3>
            <ul id={setAsideId} className="resource-grid" aria-label="Fuentes apartadas" hidden={!showSetAside}>
              {states.setAside.map((entry) => (
                <SourceCard
                  key={entry.key}
                  entry={entry}
                  onOpen={onOpen}
                  selection={cardSelection(selection, entry, visible)}
                  onDeleted={onDeleted}
                />
              ))}
            </ul>
          </section>
        )}
        {states.others.length > 0 && (
          <section className="workspace-resource-group" aria-label={GROUP_TITLES.transcript}>
            <h3>{GROUP_TITLES.transcript}</h3>
            <ul>
              {states.others.map((item) => (
                <li key={item.key}>
                  <button
                    type="button"
                    className="workspace-resource"
                    onClick={(event) => onOpen({ label: item.label, definition: item.definition }, event.currentTarget)}
                  >
                    {item.title}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
      <AddSourceToolbar subjectId={subjectId} topicId={topicId} onAdded={() => setAdded((count) => count + 1)} />
    </div>
  );
}
