import type { ReactNode } from "react";
import { kindLabel, type PendingItem, statusLabel } from "./api";

/** What each kind of doubt asks of the student, shown under the card's title. */
export const KIND_HINTS: Record<string, string> = {
  contradiction: "Tus fuentes no dicen lo mismo: habrá que elegir cuál vale.",
  possible_error: "Puede que aquí haya un error.",
  illegible: "Esta parte no se pudo leer bien.",
  incomplete: "A esta parte le falta algo.",
  unexplained_concept: "Aparece un concepto que no se explica.",
};

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** "2 páginas · 1 fragmento de la conversación · 1 fuente", or `null` without refs. */
export function describeRefs({ pages, segments, sources }: PendingItem["refs"]): string | null {
  const parts = [
    pages.length > 0 ? plural(pages.length, "página", "páginas") : null,
    segments.length > 0 ? plural(segments.length, "fragmento de la conversación", "fragmentos de la conversación") : null,
    sources.length > 0 ? plural(sources.length, "fuente", "fuentes") : null,
  ].filter((part): part is string => part !== null);
  return parts.length > 0 ? parts.join(" · ") : null;
}

/**
 * One doubt of the pending-review queue as a card: its kind (a Spanish title and what that kind
 * asks), the doubt's text, what it refers to, how many equal doubts were merged into it, and, once
 * closed, how it was closed and the resolution. `children` (the answer form of the doubt being
 * resolved, or the button that picks it) go at the end.
 */
export default function PendingCard({ item, children }: { item: PendingItem; children?: ReactNode }) {
  const label = kindLabel(item.kind);
  const refs = describeRefs(item.refs);
  const open = item.status === "open";
  return (
    <article
      aria-label={`${label}: ${item.text}`}
      className={`pending-card pending-card-${item.kind}${open ? "" : " pending-card-closed"}`}
    >
      <header>
        <h3>{label}</h3>
        <span className="pending-status">{statusLabel(item.status)}</span>
      </header>
      {open && KIND_HINTS[item.kind] && <p className="pending-hint">{KIND_HINTS[item.kind]}</p>}
      <p className="pending-text">{item.text}</p>
      {refs !== null && <p className="pending-refs">Sobre: {refs}</p>}
      {item.merged_ids.length > 0 && (
        <p className="pending-merged">
          {item.merged_ids.length === 1
            ? "Se juntó con otra duda igual."
            : `Se juntó con otras ${item.merged_ids.length} dudas iguales.`}
        </p>
      )}
      {!open && item.resolution !== null && item.resolution !== "" && (
        <p className="pending-resolution">Resolución: {item.resolution}</p>
      )}
      {children}
    </article>
  );
}
