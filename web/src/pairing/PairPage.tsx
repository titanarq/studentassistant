import QRCode from "qrcode";
import { useCallback, useEffect, useState } from "react";
import { type PairingCode, qrPayload, requestPairingCode } from "./api";

/**
 * `/pair`: shows a QR of `{url, code}` so a capture client can pair with this PC (ADR-0001).
 * The backend mints codes only for loopback callers, so the page is meant to be opened on the
 * PC itself; elsewhere it explains that instead of showing an empty QR. It only ever handles a
 * one-time pairing code, never a device token, and stores nothing.
 */

type PageState =
  | { state: "loading" }
  | { state: "ready"; pairing: PairingCode; expiresAt: number }
  | { state: "expired" }
  | { state: "refused" }
  | { state: "error" }
  | { state: "unreachable" };

function secondsLeft(expiresAt: number): number {
  return Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
}

/** Where this page lives when opened on the PC itself (same scheme and port, loopback host). */
function localPairUrl(): string {
  const { protocol, port } = window.location;
  return `${protocol}//localhost${port ? `:${port}` : ""}/pair`;
}

function formatCountdown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${rest.toString().padStart(2, "0")}`;
}

function PairingQr({ payload }: { payload: string }) {
  const [svg, setSvg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSvg(null);
    QRCode.toString(payload, { type: "svg", margin: 2, errorCorrectionLevel: "M" })
      .then((markup) => {
        if (!cancelled) setSvg(markup);
      })
      .catch(() => {
        if (!cancelled) setSvg(null);
      });
    return () => {
      cancelled = true;
    };
  }, [payload]);

  if (svg === null) return <p>Generando el código QR…</p>;
  return (
    <img
      src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`}
      alt="Código QR de emparejamiento"
      width={256}
      height={256}
    />
  );
}

function Countdown({ expiresAt, onExpired }: { expiresAt: number; onExpired: () => void }) {
  const [left, setLeft] = useState(() => secondsLeft(expiresAt));

  useEffect(() => {
    const tick = () => {
      const now = secondsLeft(expiresAt);
      setLeft(now);
      if (now === 0) onExpired();
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [expiresAt, onExpired]);

  return (
    <p aria-live="polite" data-testid="pair-countdown">
      El código caduca en {formatCountdown(left)}
    </p>
  );
}

export default function PairPage() {
  const [page, setPage] = useState<PageState>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setPage({ state: "loading" });
    requestPairingCode().then((result) => {
      if (cancelled) return;
      if (result.kind === "ok") {
        setPage({
          state: "ready",
          pairing: result.pairing,
          expiresAt: Date.parse(result.pairing.expires_at),
        });
      } else {
        setPage({ state: result.kind });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const newCode = useCallback(() => setAttempt((n) => n + 1), []);
  const expire = useCallback(() => setPage({ state: "expired" }), []);

  return (
    <main>
      <h1>Emparejar un dispositivo</h1>
      {page.state === "loading" && <p>Pidiendo un código de emparejamiento…</p>}
      {page.state === "ready" && (
        <section>
          <p>Escanea este código QR con la app de captura para emparejarla con este PC.</p>
          <PairingQr payload={qrPayload(page.pairing)} />
          <p>Si la cámara no funciona, escribe estos datos en la app:</p>
          <dl>
            <dt>Dirección</dt>
            <dd data-testid="pair-url">{page.pairing.url}</dd>
            <dt>Código</dt>
            <dd data-testid="pair-code">{page.pairing.code}</dd>
          </dl>
          <Countdown expiresAt={page.expiresAt} onExpired={expire} />
        </section>
      )}
      {page.state === "expired" && (
        <section>
          <p role="alert">El código ha caducado. Pide uno nuevo para emparejar el dispositivo.</p>
          <button type="button" onClick={newCode}>
            Generar un código nuevo
          </button>
        </section>
      )}
      {page.state === "refused" && (
        <section>
          <p role="alert">
            El servidor no genera códigos de emparejamiento para este equipo. Abre esta página en
            el propio PC donde se ejecuta Student Assistant (por ejemplo, en {localPairUrl()}).
          </p>
          <button type="button" onClick={newCode}>
            Reintentar
          </button>
        </section>
      )}
      {page.state === "error" && (
        <section>
          <p role="alert">
            El servidor respondió con un error al pedir el código de emparejamiento.
          </p>
          <button type="button" onClick={newCode}>
            Reintentar
          </button>
        </section>
      )}
      {page.state === "unreachable" && (
        <section>
          <p role="alert">
            No se pudo conectar con el servidor. Comprueba que Student Assistant está en marcha en
            este PC.
          </p>
          <button type="button" onClick={newCode}>
            Reintentar
          </button>
        </section>
      )}
    </main>
  );
}
