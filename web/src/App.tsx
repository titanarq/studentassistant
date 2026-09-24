import { useEffect, useState } from "react";
import { decodeHealthResponse } from "./protocol";

type Health =
  | { state: "loading" }
  | { state: "ok"; protocolVersion: string }
  | { state: "error" };

export default function App() {
  const [health, setHealth] = useState<Health>({ state: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetch("/api/health")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<unknown>;
      })
      .then((body) => {
        // Decoded strictly as protocol v1 `rest.health.response`; any other shape is an error.
        const health = decodeHealthResponse(body, "");
        if (!cancelled) setHealth({ state: "ok", protocolVersion: health.protocol_version });
      })
      .catch(() => {
        if (!cancelled) setHealth({ state: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main>
      <h1>Mesa de estudio</h1>
      {health.state === "loading" && <p>Conectando con el servidor…</p>}
      {health.state === "ok" && <p>Servidor en marcha, protocolo {health.protocolVersion}</p>}
      {health.state === "error" && <p role="alert">No se pudo conectar con el servidor.</p>}
    </main>
  );
}
