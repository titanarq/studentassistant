/**
 * The Recursos selection (#432): the stored sources the student ticked in the **Recursos** tab,
 * which the next workspace chat message carries as `selected_source_ids` (#433), so that
 * «incorpora el texto de estas» has a referent. It is workspace state, above the tabs, so it
 * survives switching between Captura, Recursos and the document; it is keyed by the topic-relative
 * source id the chat events use (`sources/notes/page-003.jpg`; a PDF is one source). Ids that are
 * no longer listed after a reload of the tab are dropped (`prune`).
 */

import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import { type SourceRef, sourceNumber } from "./state";

/** One selected source: its topic-relative id and the short title its chip shows. */
export interface SelectedSource {
  id: string;
  title: string;
}

/** `sources/notes/page-003.jpg`: the topic-relative id of a stored source. */
export function topicSourceId(ref: SourceRef): string {
  return `sources/${ref.kind}/${ref.file}`;
}

/** The chip's short title: «Pág. 3», «Libro p. 12», the PDF's name, the web's title. */
export function chipTitle(ref: SourceRef, title: string): string {
  const n = sourceNumber(ref.file);
  switch (ref.kind) {
    case "notes":
      return n === null ? title : `Pág. ${n}`;
    case "book":
      return n === null ? title : `Libro p. ${n}`;
    case "pdf":
      return /^PDF «(.+)»$/.exec(title)?.[1] ?? title;
    case "web":
      return title.replace(/^Web:\s*/, "");
    case "images":
      return n === null ? title : `Imagen ${n}`;
  }
}

/** `selected` with `source` added (at the end) or removed. */
export function toggled(selected: readonly SelectedSource[], source: SelectedSource): SelectedSource[] {
  return selected.some((s) => s.id === source.id) ? selected.filter((s) => s.id !== source.id) : [...selected, source];
}

/**
 * Shift-click: every source of `visible` between `anchorId` and `target` (both included) takes the
 * state `target` is toggled to. Without the anchor in `visible` it is a plain toggle.
 */
export function ranged(
  selected: readonly SelectedSource[],
  visible: readonly SelectedSource[],
  anchorId: string | null,
  target: SelectedSource,
): SelectedSource[] {
  const from = anchorId === null ? -1 : visible.findIndex((s) => s.id === anchorId);
  const to = visible.findIndex((s) => s.id === target.id);
  if (from === -1 || to === -1) return toggled(selected, target);
  const range = visible.slice(Math.min(from, to), Math.max(from, to) + 1);
  const select = !selected.some((s) => s.id === target.id);
  const ids = new Set(range.map((s) => s.id));
  if (!select) return selected.filter((s) => !ids.has(s.id));
  const have = new Set(selected.map((s) => s.id));
  return [...selected, ...range.filter((s) => !have.has(s.id))];
}

/**
 * `selected` without the ids that are not `listed` any more, and with the listed titles (a PDF's
 * name arrives with its metadata); the same array when nothing changes.
 */
export function pruned(selected: readonly SelectedSource[], listed: ReadonlyMap<string, string>): readonly SelectedSource[] {
  let changed = false;
  const next: SelectedSource[] = [];
  for (const source of selected) {
    const title = listed.get(source.id);
    if (title === undefined) {
      changed = true;
    } else if (title !== source.title) {
      changed = true;
      next.push({ id: source.id, title });
    } else {
      next.push(source);
    }
  }
  return changed ? next : selected;
}

export interface SourceSelection {
  /** In the order they were selected. */
  selected: readonly SelectedSource[];
  /** A checkbox click: `visible` is the tab's order of the sources, for a shift-click range. */
  toggle: (source: SelectedSource, visible: readonly SelectedSource[], shift: boolean) => void;
  deselect: (id: string) => void;
  clear: () => void;
  /** After a reload of the tab: `listed` maps every listed source id to its chip title. */
  prune: (listed: ReadonlyMap<string, string>) => void;
}

/** Holds the selection of one workspace page. */
export function useSourceSelection(): SourceSelection {
  const [selected, setSelected] = useState<readonly SelectedSource[]>([]);
  // The last toggled card: the other end of a shift-click range.
  const anchor = useRef<string | null>(null);

  const toggle = useCallback((source: SelectedSource, visible: readonly SelectedSource[], shift: boolean) => {
    const from = anchor.current;
    anchor.current = source.id;
    setSelected((current) => (shift ? ranged(current, visible, from, source) : toggled(current, source)));
  }, []);
  const deselect = useCallback((id: string) => setSelected((current) => current.filter((s) => s.id !== id)), []);
  const clear = useCallback(() => {
    anchor.current = null;
    setSelected([]);
  }, []);
  const prune = useCallback((listed: ReadonlyMap<string, string>) => setSelected((current) => pruned(current, listed)), []);

  return useMemo(() => ({ selected, toggle, deselect, clear, prune }), [selected, toggle, deselect, clear, prune]);
}

export const SourceSelectionContext = createContext<SourceSelection | null>(null);

/** The selection of the enclosing workspace page, or null outside one (then nothing is selectable). */
export function useSelection(): SourceSelection | null {
  return useContext(SourceSelectionContext);
}
