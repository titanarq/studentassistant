import { Fragment, type ReactNode } from "react";
import { type Block, type Inline, parseInline, parseNotes } from "../../notes/markdown";

/**
 * A written answer of the study chat (#336) rendered as light Markdown through the notes' own
 * parser (`parseNotes`/`parseInline`), so no HTML of the answer ever reaches the DOM as markup.
 * Every `[§anchor]` becomes a section chip and every `[^label]` a source chip, inline where cited;
 * what each chip shows and does is the caller's (`renderSection`, `renderSource`).
 */

/** `[§anchor]`, a section of the notes a written answer cites (#334). */
const SECTION_MARK = /\[§([A-Za-z0-9_-]+)\]/g;

export interface ReplyViewProps {
  text: string;
  renderSection: (anchor: string, key: string) => ReactNode;
  renderSource: (label: string, key: string) => ReactNode;
}

function safeHref(href: string): string | null {
  return /^(https?:|mailto:)/i.test(href) ? href : null;
}

export default function ReplyView({ text, renderSection, renderSource }: ReplyViewProps) {
  const withSections = (value: string, key: string): ReactNode[] => {
    const out: ReactNode[] = [];
    let last = 0;
    for (const match of value.matchAll(SECTION_MARK)) {
      const index = match.index ?? 0;
      if (index > last) out.push(value.slice(last, index));
      out.push(renderSection(match[1], `${key}s${index}`));
      last = index + match[0].length;
    }
    if (last < value.length) out.push(value.slice(last));
    return out;
  };

  const inline = (nodes: Inline[], key: string): ReactNode[] =>
    nodes.map((node, index) => {
      const k = `${key}${index}`;
      switch (node.type) {
        case "text": {
          const parts = withSections(node.text, k);
          return parts.length === 1 && typeof parts[0] === "string" ? parts[0] : <Fragment key={k}>{parts}</Fragment>;
        }
        case "strong":
          return <strong key={k}>{inline(node.children, `${k}.`)}</strong>;
        case "em":
          return <em key={k}>{inline(node.children, `${k}.`)}</em>;
        case "code":
          return <code key={k}>{node.text}</code>;
        case "uncertain":
          return <span key={k}>{node.text}</span>;
        case "image":
          return <span key={k}>[Imagen: {node.alt}]</span>;
        case "link": {
          const href = safeHref(node.href);
          const children = inline(node.children, `${k}.`);
          return href === null ? (
            <span key={k}>{children}</span>
          ) : (
            <a key={k} href={href} rel="noopener noreferrer" target="_blank">
              {children}
            </a>
          );
        }
        case "footnote":
          return renderSource(node.label, k);
      }
    });

  const line = (value: string, key: string) => inline(parseInline(value), key);

  const block = (b: Block, key: string): ReactNode => {
    switch (b.type) {
      case "heading":
        return (
          <p key={key} className="study-reply-heading">
            <strong>{line(b.text, key)}</strong>
          </p>
        );
      case "paragraph":
        return <p key={key}>{line(b.text, key)}</p>;
      case "list": {
        const items = b.items.map((item, index) => {
          const only = item.length === 1 && item[0].type === "paragraph" ? item[0] : null;
          return (
            <li key={`${key}.${index}`}>
              {only ? line(only.text, `${key}.${index}.`) : item.map((child, c) => block(child, `${key}.${index}.${c}`))}
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
      case "table":
        return (
          <div key={key} className="notes-table">
            <table>
              <thead>
                <tr>
                  {b.header.map((cell, c) => (
                    <th key={c} scope="col">
                      {line(cell, `${key}.h${c}.`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => (
                      <td key={c}>{line(cell, `${key}.${r}.${c}.`)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case "rule":
        return <hr key={key} />;
      case "code":
        return (
          <pre key={key}>
            <code>{b.text}</code>
          </pre>
        );
      case "quote":
        return <blockquote key={key}>{b.blocks.map((child, c) => block(child, `${key}.${c}`))}</blockquote>;
    }
  };

  return <div className="study-reply">{parseNotes(text).blocks.map((b, index) => block(b, String(index)))}</div>;
}
