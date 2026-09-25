import { diffStats, parseDiff } from "./diff";

/**
 * The change a chat turn applied to the notes, as a highlighted unified diff: added lines in
 * `<ins>`, removed ones in `<del>`, hunk headers dimmed. Open by default; the summary says how
 * many lines were added and removed.
 */
export default function DiffView({ diff }: { diff: string }) {
  const lines = parseDiff(diff);
  if (lines.length === 0) return null;
  const { added, removed } = diffStats(lines);
  return (
    <details className="chat-diff" open>
      <summary>
        Cambios en los apuntes ({added} {added === 1 ? "línea añadida" : "líneas añadidas"}, {removed}{" "}
        {removed === 1 ? "quitada" : "quitadas"})
      </summary>
      <pre>
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
                  {line.text}
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
    </details>
  );
}
