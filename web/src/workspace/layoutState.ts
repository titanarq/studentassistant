/**
 * The study screens' layout choices kept in `sessionStorage` (#534): the width of the chat column
 * (shared by Construir and Estudiar) and whether the left card is collapsed. Every access is
 * wrapped: the storage can be missing, full or blocked, and then the screen uses its defaults.
 */

/** The chat column's share of the screen width (percent) when nothing was chosen. */
export const DEFAULT_SIDE_PERCENT = 40;
export const MIN_SIDE_PERCENT = 25;
export const MAX_SIDE_PERCENT = 65;

const SIDE_KEY = "studentassistant.workspace.sidePercent";
const COLLAPSED_KEY = "studentassistant.workspace.sourcesCollapsed";

export function clampSide(percent: number): number {
  if (!Number.isFinite(percent)) return DEFAULT_SIDE_PERCENT;
  return Math.min(MAX_SIDE_PERCENT, Math.max(MIN_SIDE_PERCENT, percent));
}

function read(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // No storage: the choice only lasts while the page is open.
  }
}

export function readSidePercent(): number {
  const raw = read(SIDE_KEY);
  if (raw === null || raw.trim() === "") return DEFAULT_SIDE_PERCENT;
  const value = Number(raw);
  return Number.isFinite(value) ? clampSide(value) : DEFAULT_SIDE_PERCENT;
}

export function writeSidePercent(percent: number): void {
  write(SIDE_KEY, String(Math.round(clampSide(percent) * 10) / 10));
}

/** The remembered collapsed state of one topic's left card; null when never chosen. */
export function readSourcesCollapsed(topicKey: string): boolean | null {
  const raw = read(`${COLLAPSED_KEY}.${topicKey}`);
  return raw === "1" ? true : raw === "0" ? false : null;
}

export function writeSourcesCollapsed(topicKey: string, collapsed: boolean): void {
  write(`${COLLAPSED_KEY}.${topicKey}`, collapsed ? "1" : "0");
}
