import { type DiffLine, diffStats, parseDiff } from "../chat/diff";
import type { SectionDiff, SectionStatus, VersionDiff } from "./api";
import { type SideCell, sideBySide } from "./sideBySide";

/**
 * Two notes versions compared by section (`VersionDiff`): one `article` per section that changed,
 * in the newer side's order, named after its title and status, with its unified diff inline or
 * side by side (`layout`); the sections left as they were are only listed, in a closed `details`;
 * the footnote labels added, removed or changed are listed under "Fuentes citadas".
 */

export type DiffLayout = "inline" | "side";

export const STATUS_LABEL: Record<SectionStatus, string> = {
  added: "Nueva",
  removed: "Quitada",
  changed: "Modificada",
  unchanged: "Sin cambios",
};

/** The section's title on the newer side (the older one for a removed section). */
export function sectionTitle(section: SectionDiff): string {
  if (section.key === "") return "Inicio de los apuntes";
  return section.title_after ?? section.title_before ?? `#${section.key}`;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function InlineDiff({ lines }: { lines: DiffLine[] }) {
  return (
    <pre className="version-diff-inline">
      {lines.map((line, index) => {
        switch (line.kind) {
          case "add":
            return (
              <ins key={index} className="diff-add">
                <span aria-hidden="true">+ </span>
                {line.text}
              </ins>
            );
          case "del":
            return (
              <del key={index} className="diff-del">
                <span aria-hidden="true">- </span>
                {line.text}
              </del>
            );
          case "hunk":
            return (
              <span key={index} className="diff-hunk" aria-hidden="true">
                ⋯
              </span>
            );
          case "context":
            return (
              <span key={index} className="diff-context">
                {"  "}
                {line.text}
              </span>
            );
        }
      })}
    </pre>
  );
}

function Cell({ cell }: { cell: SideCell }) {
  if (cell === null) return <td className="diff-empty" />;
  if (cell.kind === "del")
    return (
      <td className="diff-del">
        <del>{cell.text}</del>
      </td>
    );
  if (cell.kind === "add")
    return (
      <td className="diff-add">
        <ins>{cell.text}</ins>
      </td>
    );
  return <td className="diff-context">{cell.text}</td>;
}

function SideBySideDiff({ lines, before, after }: { lines: DiffLine[]; before: string; after: string }) {
  return (
    <div className="version-diff-side">
      <table>
        <thead>
          <tr>
            <th scope="col">{before}</th>
            <th scope="col">{after}</th>
          </tr>
        </thead>
        <tbody>
          {sideBySide(lines).map((row, index) =>
            row.kind === "hunk" ? (
              <tr key={index} className="diff-hunk" aria-hidden="true">
                <td colSpan={2}>⋯</td>
              </tr>
            ) : (
              <tr key={index}>
                <Cell cell={row.left} />
                <Cell cell={row.right} />
              </tr>
            ),
          )}
        </tbody>
      </table>
    </div>
  );
}

function SectionChange({
  section,
  layout,
  before,
  after,
}: {
  section: SectionDiff;
  layout: DiffLayout;
  before: string;
  after: string;
}) {
  const title = sectionTitle(section);
  const lines = parseDiff(section.diff);
  const { added, removed } = diffStats(lines);
  const notes: string[] = [];
  if (section.renamed && section.title_before !== null) notes.push(`renombrada, antes «${section.title_before}»`);
  if (section.moved) notes.push("cambiada de sitio");
  return (
    <article className={`version-section version-${section.status}`} aria-label={`${title}: ${STATUS_LABEL[section.status]}`}>
      <h3>
        {title} <span className="version-status">{STATUS_LABEL[section.status]}</span>
      </h3>
      {notes.length > 0 && <p className="version-section-notes">Sección {notes.join(", ")}.</p>}
      {lines.length > 0 && (
        <p className="version-section-stats">
          {plural(added, "línea añadida", "líneas añadidas")}, {plural(removed, "quitada", "quitadas")}
        </p>
      )}
      {lines.length > 0 &&
        (layout === "side" ? (
          <SideBySideDiff lines={lines} before={before} after={after} />
        ) : (
          <InlineDiff lines={lines} />
        ))}
    </article>
  );
}

export function versionName(version: number | null): string {
  return version === null ? "Apuntes actuales" : `Versión ${version}`;
}

export default function VersionDiffView({ diff, layout }: { diff: VersionDiff; layout: DiffLayout }) {
  const before = versionName(diff.from_version);
  const after = versionName(diff.to_version);
  if (diff.identical) return <p>{before} y {after.toLowerCase()} son iguales.</p>;
  const changed = diff.sections.filter((s) => s.status !== "unchanged");
  const unchanged = diff.sections.filter((s) => s.status === "unchanged" && !s.moved);
  const { added, removed, changed: edited } = diff.footnotes;
  return (
    <div className="version-diff">
      <p>
        {changed.length === 0
          ? "Ninguna sección cambia de texto."
          : `${plural(changed.length, "sección cambia", "secciones cambian")} de ${before.toLowerCase()} a ${after.toLowerCase()}.`}
      </p>
      {diff.sections
        .filter((s) => s.status !== "unchanged" || s.moved)
        .map((section) => (
          <SectionChange key={section.key} section={section} layout={layout} before={before} after={after} />
        ))}
      {unchanged.length > 0 && (
        <details className="version-unchanged">
          <summary>{plural(unchanged.length, "sección sin cambios", "secciones sin cambios")}</summary>
          <ul>
            {unchanged.map((section) => (
              <li key={section.key}>{sectionTitle(section)}</li>
            ))}
          </ul>
        </details>
      )}
      {added.length + removed.length + edited.length > 0 && (
        <section aria-label="Fuentes citadas" className="version-footnotes">
          <h3>Fuentes citadas</h3>
          <ul>
            {added.length > 0 && <li>Nuevas: {added.map((l) => `[^${l}]`).join(", ")}</li>}
            {removed.length > 0 && <li>Quitadas: {removed.map((l) => `[^${l}]`).join(", ")}</li>}
            {edited.length > 0 && <li>Modificadas: {edited.map((l) => `[^${l}]`).join(", ")}</li>}
          </ul>
        </section>
      )}
    </div>
  );
}
