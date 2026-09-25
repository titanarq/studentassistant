import type { TopicSummary } from "../desk/api";

/**
 * The topic card of docs/VISION.md §2: sources by kind, sessions and minutes of conversation,
 * doubts pending review, and the notes version and generated material (✓ present, ○ not yet).
 */

/** Generated material in the card's order, recognised by the file name under `generated/`. */
export const MATERIALS: { label: string; stem: RegExp }[] = [
  { label: "Esquema", stem: /^(outline|esquema)\b/i },
  { label: "Quiz", stem: /^quiz\b/i },
  { label: "Flashcards", stem: /^flashcards?\b/i },
  { label: "Examen", stem: /^(exam|examen|exercises|ejercicios)\b/i },
  { label: "Diapositivas", stem: /^(slides|diapositivas)\b/i },
];

function hasMaterial(generated: string[], stem: RegExp): boolean {
  return generated.some((path) => stem.test(path.slice(path.lastIndexOf("/") + 1)));
}

function mark(present: boolean): string {
  return present ? "✓" : "○";
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function sourceItems({ notes, book, pdf, web }: TopicSummary["sources"]): string[] {
  return [
    `${mark(notes > 0)} ${plural(notes, "página manuscrita", "páginas manuscritas")}`,
    `${mark(book > 0)} ${plural(book, "página del libro", "páginas del libro")}`,
    `${mark(pdf > 0)} ${plural(pdf, "PDF", "PDF")}`,
    `${mark(web > 0)} ${plural(web, "web", "webs")}`,
  ];
}

function formatMinutes(minutes: number): string {
  const rounded = Math.round(minutes);
  return rounded === 1 ? "1 min" : `${rounded} min`;
}

export default function TopicCard({ summary }: { summary: TopicSummary }) {
  const { sessions, session_minutes, open_pending, notes_version, generated } = summary;
  const materials = [
    notes_version !== null ? `✓ Apuntes v${notes_version}` : "○ Apuntes",
    ...MATERIALS.map(({ label, stem }) => `${mark(hasMaterial(generated, stem))} ${label}`),
  ];
  return (
    <section aria-label="Resumen del tema">
      <dl>
        <dt>Fuentes</dt>
        <dd>{sourceItems(summary.sources).join("  ")}</dd>
        <dt>Sesiones</dt>
        <dd>
          {sessions === 0
            ? "○ Ninguna todavía"
            : `✓ ${sessions} (${formatMinutes(session_minutes)} de conversación)`}
        </dd>
        <dt>Pendiente</dt>
        <dd>
          {open_pending === 0
            ? "Nada por revisar"
            : plural(open_pending, "duda por revisar", "dudas por revisar")}
        </dd>
        <dt>Material</dt>
        <dd>{materials.join("  ")}</dd>
      </dl>
    </section>
  );
}
