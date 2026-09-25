import type { ReactNode } from "react";
import { type Block, footnoteRefs, IA_LABEL, type Inline, type NotesTree, parseInline } from "./markdown";
import { parseProvenance } from "./provenance";

/**
 * The master notes rendered from `parseNotes`: headings carry their stable anchor as `id` (with a
 * `#` link to it), every provenance footnote reference is a link to its definition that opens the
 * sources panel instead (`onOpenSource`), and every block that cites `[^ia]` is highlighted as
 * content the AI added. The definitions are listed at the end under "Fuentes".
 *
 * With `changedSections`, every top-level block inside one of those sections (or a subsection of
 * one) is highlighted as changed by the editor's last turn; with `onAskWhy`, every top-level block
 * with text gets a "¿Por qué pusiste esto?" button that hands the block and its section's anchor
 * to the caller.
 */

export interface NotesViewProps {
  tree: NotesTree;
  /** A footnote reference or definition was activated; `trigger` gets the focus back on close. */
  onOpenSource: (label: string, trigger: HTMLElement) => void;
  /** The label whose source the panel shows, marked as current. */
  activeLabel?: string | null;
  /** Anchors of the sections the editor's last turn changed, highlighted. */
  changedSections?: ReadonlySet<string>;
  /**
   * "¿Por qué pusiste esto?" on a block: the block, the anchor of its section, if any, and its
   * number in that section as the backend counts blocks (from 1, after the section heading; before
   * the first section, from the start of the notes, the `# title` included).
   */
  onAskWhy?: (block: Block, section: string | null, number: number) => void;
  /** The "¿Por qué?" buttons are disabled (the editor is busy). */
  askDisabled?: boolean;
}

const refId = (label: string, n: number) => `fnref-${label}-${n}`;
const defId = (label: string) => `fn-${label}`;

/** The number each cited label shows, in order of first citation; `[^ia]` shows "IA". */
function numbering(tree: NotesTree): Map<string, string> {
  const numbers = new Map<string, string>();
  let next = 1;
  const visit = (text: string) => {
    for (const label of footnoteRefs(text)) {
      if (numbers.has(label)) continue;
      numbers.set(label, label === IA_LABEL ? "IA" : String(next++));
    }
  };
  const walk = (blocks: Block[]) => {
    for (const block of blocks) {
      if (block.type === "heading" || block.type === "paragraph") visit(block.text);
      else if (block.type === "table") [block.header, ...block.rows].flat().forEach(visit);
      else if (block.type === "list") block.items.forEach(walk);
      else if (block.type === "quote") walk(block.blocks);
    }
  };
  walk(tree.blocks);
  for (const { label } of tree.footnotes) {
    if (!numbers.has(label)) numbers.set(label, label === IA_LABEL ? "IA" : String(next++));
  }
  return numbers;
}

function citesIa(text: string): boolean {
  return footnoteRefs(text).includes(IA_LABEL);
}

function safeHref(href: string): string | null {
  return /^(https?:|mailto:|#)/i.test(href) ? href : null;
}

export default function NotesView({
  tree,
  onOpenSource,
  activeLabel = null,
  changedSections,
  onAskWhy,
  askDisabled = false,
}: NotesViewProps) {
  const numbers = numbering(tree);
  const definitions = new Map(tree.footnotes.map((f) => [f.label, f.text]));
  const seen = new Map<string, number>();

  const describe = (label: string): string => {
    const definition = definitions.get(label);
    if (definition === undefined) return `Fuente ${label} sin definir`;
    const provenance = parseProvenance(label, definition);
    if (label === IA_LABEL) return "Ampliado por la IA";
    return provenance.kind === "web" ? `Fuente externa (web): ${provenance.text}` : `Fuente: ${provenance.text}`;
  };
  // Web snapshots are external sources (#59): marked apart from the student's own notes and books.
  const isExternal = (label: string): boolean => {
    const definition = definitions.get(label);
    return definition !== undefined && parseProvenance(label, definition).kind === "web";
  };
  const refClass = (label: string): string =>
    label === IA_LABEL ? "notes-ref notes-ref-ia" : isExternal(label) ? "notes-ref notes-ref-web" : "notes-ref";

  const inline = (nodes: Inline[], key = ""): ReactNode[] =>
    nodes.map((node, index) => {
      const k = `${key}${index}`;
      switch (node.type) {
        case "text":
          return node.text;
        case "strong":
          return <strong key={k}>{inline(node.children, `${k}.`)}</strong>;
        case "em":
          return <em key={k}>{inline(node.children, `${k}.`)}</em>;
        case "code":
          return <code key={k}>{node.text}</code>;
        case "uncertain":
          return (
            <span key={k} className="notes-uncertain" title="Palabra dudosa en la transcripción">
              {node.text}
            </span>
          );
        case "link": {
          const href = safeHref(node.href);
          const children = inline(node.children, `${k}.`);
          return href === null ? (
            <span key={k}>{children}</span>
          ) : (
            <a key={k} href={href} rel="noopener noreferrer" target={href.startsWith("#") ? undefined : "_blank"}>
              {children}
            </a>
          );
        }
        case "footnote": {
          const n = (seen.get(node.label) ?? 0) + 1;
          seen.set(node.label, n);
          const number = numbers.get(node.label) ?? "?";
          return (
            <sup key={k} className={refClass(node.label)}>
              <a
                id={refId(node.label, n)}
                href={`#${defId(node.label)}`}
                aria-label={describe(node.label)}
                aria-current={activeLabel === node.label ? "true" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  onOpenSource(node.label, event.currentTarget);
                }}
              >
                [{number}]
              </a>
            </sup>
          );
        }
      }
    });

  const text = (value: string, key: string) => inline(parseInline(value), key);

  const block = (b: Block, key: string): ReactNode => {
    switch (b.type) {
      case "heading": {
        const Tag = `h${Math.min(b.level, 6)}` as "h1";
        return (
          <Tag key={key} id={b.anchor ?? undefined}>
            {text(b.text, key)}
            {b.anchor !== null && (
              <>
                {" "}
                <a className="notes-anchor" href={`#${b.anchor}`} aria-label={`Enlace a la sección ${b.anchor}`}>
                  #
                </a>
              </>
            )}
          </Tag>
        );
      }
      case "paragraph":
        return citesIa(b.text) ? (
          <p key={key} className="notes-ia" aria-description="Ampliado por la IA: no está en tus fuentes">
            {text(b.text, key)}
          </p>
        ) : (
          <p key={key}>{text(b.text, key)}</p>
        );
      case "list": {
        const items = b.items.map((item, index) => {
          const ia = item.some((child) => child.type === "paragraph" && citesIa(child.text));
          const only = item.length === 1 && item[0].type === "paragraph" ? item[0] : null;
          return (
            <li key={`${key}.${index}`} className={ia ? "notes-ia" : undefined}>
              {only ? text(only.text, `${key}.${index}.`) : item.map((child, c) => block(child, `${key}.${index}.${c}`))}
            </li>
          );
        });
        return b.ordered ? (
          <ol key={key} start={b.start === 1 ? undefined : b.start}>
            {items}
          </ol>
        ) : (
          <ul key={key}>{items}</ul>
        );
      }
      case "table": {
        const ia = [b.header, ...b.rows].flat().some(citesIa);
        return (
          <div key={key} className="notes-table">
            <table className={ia ? "notes-ia" : undefined}>
              <thead>
                <tr>
                  {b.header.map((cell, c) => (
                    <th key={c} scope="col">
                      {text(cell, `${key}.h${c}.`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => (
                      <td key={c}>{text(cell, `${key}.${r}.${c}.`)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      }
      case "rule":
        return <hr key={key} />;
      case "code":
        return (
          <pre key={key} className="notes-code">
            <code>{b.text}</code>
          </pre>
        );
      case "quote":
        return <blockquote key={key}>{b.blocks.map((child, c) => block(child, `${key}.${c}`))}</blockquote>;
    }
  };

  const wrap = changedSections !== undefined || onAskWhy !== undefined;
  const headings: { level: number; anchor: string | null }[] = [];
  let number = 0;
  const body = tree.blocks.map((b, index) => {
    const key = String(index);
    if (!wrap) return block(b, key);
    if (b.type === "heading" && b.level >= 2) number = 0;
    else number += 1;
    const blockNumber = number;
    if (b.type === "heading") {
      while (headings.length > 0 && headings[headings.length - 1].level >= b.level) headings.pop();
      headings.push({ level: b.level, anchor: b.anchor });
    }
    const section = [...headings].reverse().find((h) => h.anchor !== null)?.anchor ?? null;
    const changed = headings.some((h) => h.anchor !== null && changedSections?.has(h.anchor));
    const askable = onAskWhy !== undefined && b.type !== "heading" && b.type !== "rule";
    return (
      <div
        key={key}
        className={changed ? "notes-block notes-changed" : "notes-block"}
        aria-description={changed && b.type !== "heading" ? "Cambiado por el editor en el último mensaje" : undefined}
      >
        {block(b, key)}
        {askable && (
          <button
            type="button"
            className="notes-why"
            aria-label="¿Por qué pusiste esto?"
            title="¿Por qué pusiste esto?"
            disabled={askDisabled}
            onClick={() => onAskWhy(b, section, blockNumber)}
          >
            ¿Por qué?
          </button>
        )}
      </div>
    );
  });
  const cited = [...numbers.keys()].filter((label) => definitions.has(label));

  return (
    <article className="notes-body" aria-label="Apuntes">
      {body}
      {cited.length > 0 && (
        <section className="notes-footnotes" aria-labelledby="notes-footnotes-heading">
          <h2 id="notes-footnotes-heading">Fuentes</h2>
          <ol>
            {cited.map((label) => {
              const provenance = parseProvenance(label, definitions.get(label) as string);
              return (
                <li
                  key={label}
                  id={defId(label)}
                  className={provenance.kind === "web" ? "notes-footnote-external" : undefined}
                  value={label === IA_LABEL ? undefined : Number(numbers.get(label))}
                >
                  <span className="notes-footnote-number">[{numbers.get(label)}]</span>{" "}
                  <a
                    href={`#${defId(label)}`}
                    onClick={(event) => {
                      event.preventDefault();
                      onOpenSource(label, event.currentTarget);
                    }}
                  >
                    {provenance.text}
                  </a>
                  {provenance.kind === "web" && <span className="notes-external-tag"> · fuente externa</span>}
                </li>
              );
            })}
          </ol>
        </section>
      )}
    </article>
  );
}
