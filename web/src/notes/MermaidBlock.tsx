import { useEffect, useState } from "react";
import { renderMermaid } from "./mermaid";
import { useDarkScheme } from "./useColorScheme";

type State = { status: "loading" } | { status: "done"; svg: string } | { status: "failed" };

/**
 * A ```mermaid fence of the notes drawn as a diagram (#479). Until mermaid has drawn it, and for
 * good when mermaid cannot parse it, the source shows as the code block every other fence is,
 * so a broken diagram never hides what the student wrote nor breaks the rest of the notes.
 *
 * The SVG is mermaid's own output under `securityLevel: 'strict'`, which sanitizes every label;
 * it is the only markup of the notes inserted as HTML.
 */
export default function MermaidBlock({ source }: { source: string }) {
  const dark = useDarkScheme();
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    let current = true;
    renderMermaid(source, dark ? "dark" : "default").then(
      (svg) => current && setState({ status: "done", svg }),
      () => current && setState({ status: "failed" }),
    );
    return () => {
      current = false;
    };
  }, [source, dark]);

  if (state.status === "done") {
    return (
      <figure
        className="notes-mermaid"
        data-testid="notes-mermaid"
        dangerouslySetInnerHTML={{ __html: state.svg }}
      />
    );
  }
  return (
    <div className="notes-mermaid-source">
      <pre className="notes-code">
        <code>{source}</code>
      </pre>
      {state.status === "failed" && <p className="notes-mermaid-error">No se pudo dibujar el diagrama</p>}
    </div>
  );
}
