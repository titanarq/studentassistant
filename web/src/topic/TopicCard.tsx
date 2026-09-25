import { type TopicSummary, topicPath } from "../desk/api";

/**
 * The topic card of docs/VISION.md §2: sources by kind, sessions and minutes of conversation,
 * doubts pending review, and the notes version and generated material (✓ present, ○ not yet).
 * The notes item, once a version exists, links to the notes viewer (`<topic path>/notes`), and
 * the pending item to the pending-doubts panel (`<topic path>/pending`); beside the notes item,
 * "versiones" links to the notes version history (`<topic path>/versions`). Generated files
 * meant to be taken elsewhere (the Anki deck, a CSV, a PDF...) are listed under "Descargas" as
 * links to `GET /api<topic path>/generated/files/<name>`.
 */

/** Generated material in the card's order, recognised by the file name under `generated/`. */
export const MATERIALS: { label: string; stem: RegExp }[] = [
  { label: "Esquema", stem: /^(outline|esquema)\b/i },
  { label: "Quiz", stem: /^quiz\b/i },
  { label: "Flashcards", stem: /^flashcards?\b/i },
  { label: "Examen", stem: /^(exam|examen|exercises|ejercicios)\b/i },
  { label: "Diapositivas", stem: /^(slides|diapositivas)\b/i },
];

/** Generated files offered for download, by extension, with the label shown beside the name. */
const DOWNLOADS: Record<string, string> = { apkg: "Anki", csv: "CSV", pdf: "PDF", pptx: "PowerPoint" };

interface Download {
  name: string;
  label: string;
}

/** The downloadable files among `generated` (vault-relative paths), by their name under `generated/`. */
export function downloads(generated: string[]): Download[] {
  const found: Download[] = [];
  for (const path of generated) {
    const at = path.indexOf("/generated/");
    if (at < 0) continue;
    const name = path.slice(at + "/generated/".length);
    const dot = name.lastIndexOf(".");
    const label = dot > 0 ? DOWNLOADS[name.slice(dot + 1).toLowerCase()] : undefined;
    if (label !== undefined) found.push({ name, label: `${name.slice(0, dot)} (${label})` });
  }
  return found;
}

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
  const materials = MATERIALS.map(({ label, stem }) => `${mark(hasMaterial(generated, stem))} ${label}`);
  const files = downloads(generated);
  const filesBase = `/api${topicPath(summary.subject_id, summary.topic_id)}/generated/files`;
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
          <a href={`${topicPath(summary.subject_id, summary.topic_id)}/pending`}>
            {open_pending === 0
              ? "Nada por revisar"
              : plural(open_pending, "duda por revisar", "dudas por revisar")}
          </a>
        </dd>
        <dt>Material</dt>
        <dd>
          {notes_version !== null ? (
            <>
              ✓ <a href={`${topicPath(summary.subject_id, summary.topic_id)}/notes`}>Apuntes v{notes_version}</a> (
              <a href={`${topicPath(summary.subject_id, summary.topic_id)}/versions`}>versiones</a>)
            </>
          ) : (
            "○ Apuntes"
          )}
          {`  ${materials.join("  ")}`}
        </dd>
        {files.length > 0 && (
          <>
            <dt>Descargas</dt>
            <dd>
              {files.map(({ name, label }, index) => (
                <span key={name}>
                  {index > 0 && "  "}
                  <a href={`${filesBase}/${name.split("/").map(encodeURIComponent).join("/")}`} download>
                    {label}
                  </a>
                </span>
              ))}
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}
