import { useEffect, useState } from "react";

type Health =
  | { state: "loading" }
  | { state: "ok"; version: string }
  | { state: "error" };

export default function App() {
  const [health, setHealth] = useState<Health>({ state: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetch("/api/health")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<{ status: string; version: string }>;
      })
      .then((body) => {
        if (!cancelled) setHealth({ state: "ok", version: body.version });
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
      {health.state === "ok" && <p>Servidor en marcha, versión {health.version}</p>}
      {health.state === "error" && <p role="alert">No se pudo conectar con el servidor.</p>}
    </main>
  );
}
