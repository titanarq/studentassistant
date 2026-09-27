import { type MouseEvent, useEffect, useMemo, useState } from "react";
import { describeFailure, fetchTopicSummary, type ReadResult, type TopicSummary } from "../desk/api";
import type { NotesTree } from "../notes/markdown";
import SourcePanel from "../notes/SourcePanel";
import { sourceUrl } from "../notes/api";
import AddSource from "./resources/AddSource";
import { fetchTopicSources, GROUP_TITLES, resourceList, type TopicSources } from "./resources";
import { countsText, resourceStates, STATE_LABELS, type SourceEntry, thumbnailOf } from "./resources/state";
import { chipTitle, type SelectedSource, type SourceSelection, topicSourceId, useSelection } from "./resources/selection";
import { useSourceMetas } from "./resources/useSourceMetas";

/** The source open in the tab: a footnote label and its definition. */
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
  open: OpenResource | null;
  onOpen: (resource: OpenResource) => void;
  onClose: () => void;
}

/** What the tab read: the topic's source list, or the summary when the backend has no list. */
type Loaded = { kind: "listed"; value: TopicSources } | ReadResult<TopicSummary>;

/** `GET .../sources`, falling back to `GET .../summary` when it does not answer a list (#323). */
async function loadSources(subjectId: string, topicId: string): Promise<Loaded> {
  const listed = await fetchTopicSources(subjectId, topicId);
  if (listed.kind === "ok") return { kind: "listed", value: listed.value };
  return fetchTopicSummary(subjectId, topicId);
}

export const RESOURCES_HINT =
  "Selecciona páginas y pídeselo al asistente en el chat (p. ej. «incorpora el texto de estas»).";

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

function SourceCard({
  entry,
  onOpen,
  selection,
}: {
  entry: SourceEntry;
  onOpen: (resource: OpenResource) => void;
  selection: CardSelection | null;
}) {
  const chip = entry.state === "incorporated" ? "badge badge-ok" : entry.state === "pending" ? "badge badge-warn" : "badge";
  const reasons = entry.reasons.join(", ");
  const detail = entry.state === "set_aside" ? [reasons, entry.byStudent ? "(la apartaste tú)" : ""].filter((part) => part !== "").join(" ") : "";
  const selected = selection?.selected === true;
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
      <button type="button" onClick={() => onOpen({ label: entry.label, definition: entry.definition })}>
        <Thumbnail entry={entry} />
        <span className="resource-title">{entry.title}</span>
        <span className="resource-state">
          <span className={chip}>{STATE_LABELS[entry.state]}</span>
          {detail !== "" && <span className="resource-reason"> {detail}</span>}
        </span>
        {entry.flagged && reasons !== "" && <span className="resource-warning">Aviso: {reasons}</span>}
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
 * The **Recursos** tab (#312, #328, #323): every source of the topic (`GET .../sources`, or the
 * summary's counts on an older backend) with its state -- **Pendiente**,
 * **Incorporada** (cited by the current notes) or **Apartada** (set aside by the capture triage
 * or by the student, with the reason) -- kept ones first in source order, then a collapsed
 * "Apartadas (N)" group. Above the list, «Añadir fuente» (`AddSource`, #384) adds a PDF, a web
 * page or the textbook's title and then the list is read again. There are no per-source action
 * buttons: incorporating, setting aside and restoring
 * are asked in the chat. Each stored source has a checkbox (#432): the ticked ones are the
 * workspace's selection (`resources/selection.ts`), shown as chips above the chat input and sent
 * with the next message; Shift-click ticks a range, «Quitar selección» clears it. Choosing a source (here or from a footnote of the document) shows it in
 * the sources viewer (`SourcePanel`) in the list's place; "Cerrar" goes back to the list. The
 * sources' metadata is read again each time the tab is shown and after each change of the notes.
 */
export default function ResourcesTab({ subjectId, topicId, tree, refreshKey, open, onOpen, onClose }: ResourcesTabProps) {
  const [summary, setSummary] = useState<Loaded | null>(null);
  const [showSetAside, setShowSetAside] = useState(false);
  // Bumped after «Añadir fuente» added something: the list is read again, as `refreshKey` does.
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
  const first = useMemo(() => resourceStates(subjectId, topicId, items, tree, new Map()), [subjectId, topicId, items, tree]);
  const vaultIds = useMemo(() => [...first.kept, ...first.setAside].map((entry) => entry.vaultId), [first]);
  const metas = useSourceMetas(vaultIds, reloadKey);
  const states = useMemo(() => resourceStates(subjectId, topicId, items, tree, metas), [subjectId, topicId, items, tree, metas]);
  const selection = useSelection();
  // The visible order of the cards (kept, then set aside), for a shift-click range.
  const visible = useMemo(() => [...states.kept, ...(showSetAside ? states.setAside : [])].map(selectedOf), [states, showSetAside]);
  const listed = summary?.kind === "listed" || (summary?.kind === "ok" && tree !== null);
  const prune = selection?.prune;
  useEffect(() => {
    if (!listed || prune === undefined) return;
    prune(new Map([...states.kept, ...states.setAside].map((entry) => [topicSourceId(entry.ref), chipTitle(entry.ref, entry.title)])));
  }, [listed, prune, states]);

  if (open !== null) {
    return (
      <div className="workspace-resources">
        <SourcePanel subjectId={subjectId} topicId={topicId} label={open.label} definition={open.definition} onClose={onClose} />
      </div>
    );
  }

  const total = states.kept.length + states.setAside.length;
  const setAsideId = "workspace-resources-set-aside";

  return (
    <div className="workspace-resources">
      <AddSource subjectId={subjectId} topicId={topicId} onAdded={() => setAdded((count) => count + 1)} />
      {summary === null && <p>Cargando las fuentes…</p>}
      {summary !== null && summary.kind !== "ok" && summary.kind !== "listed" && (
        <p role="alert">No se pudieron cargar las fuentes del tema: {describeFailure(summary)}</p>
      )}
      {summary !== null && total === 0 && states.others.length === 0 && list.uncitedWebs === 0 && (
        <p>Este tema todavía no tiene fuentes: captura páginas en la pestaña Captura.</p>
      )}
      {total > 0 && (
        <>
          <p className="resources-counts">{countsText(states.counts)}</p>
          <p className="resources-hint">{RESOURCES_HINT}</p>
        </>
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
            <SourceCard key={entry.key} entry={entry} onOpen={onOpen} selection={cardSelection(selection, entry, visible)} />
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
              <SourceCard key={entry.key} entry={entry} onOpen={onOpen} selection={cardSelection(selection, entry, visible)} />
            ))}
          </ul>
        </section>
      )}
      {list.uncitedWebs > 0 && (
        <p className="workspace-resources-note">
          {list.uncitedWebs === 1
            ? "Hay 1 web guardada que los apuntes todavía no citan."
            : `Hay ${list.uncitedWebs} webs guardadas que los apuntes todavía no citan.`}
        </p>
      )}
      {states.others.length > 0 && (
        <section className="workspace-resource-group" aria-label={GROUP_TITLES.transcript}>
          <h3>
            {GROUP_TITLES.transcript} <span className="badge">{states.others.length}</span>
          </h3>
          <ul>
            {states.others.map((item) => (
              <li key={item.key}>
                <button
                  type="button"
                  className="workspace-resource"
                  onClick={() => onOpen({ label: item.label, definition: item.definition })}
                >
                  {item.title}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
