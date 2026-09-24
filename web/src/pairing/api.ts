/**
 * Client for `POST /api/pair/codes` (#89): asks the backend on this PC for a one-time pairing
 * code. The backend answers only loopback callers, so the `/pair` page works only when it is
 * opened on the PC itself. This module never sees a device token: only the pairing code.
 */

/** The backend's answer: what the pairing QR carries (`url`, `code`) plus the expiry. */
export interface PairingCode {
  url: string;
  code: string;
  /** ISO 8601 instant after which the code can no longer be redeemed. */
  expires_at: string;
}

export type PairingCodeResult =
  | { kind: "ok"; pairing: PairingCode }
  /** 403: the backend mints codes only for callers on the PC itself. */
  | { kind: "refused" }
  /** Any other non-2xx status, or a 2xx body that is not a pairing code. */
  | { kind: "error"; status: number }
  /** The request never got an answer (backend down, network error). */
  | { kind: "unreachable" };

export const PAIRING_CODES_PATH = "/api/pair/codes";

function isPairingCode(body: unknown): body is PairingCode {
  if (typeof body !== "object" || body === null) return false;
  const { url, code, expires_at } = body as Record<string, unknown>;
  return (
    typeof url === "string" &&
    url.length > 0 &&
    typeof code === "string" &&
    code.length > 0 &&
    typeof expires_at === "string" &&
    !Number.isNaN(Date.parse(expires_at))
  );
}

/** The exact text the pairing QR encodes: the `{url, code}` of the backend's answer. */
export function qrPayload(pairing: PairingCode): string {
  return JSON.stringify({ url: pairing.url, code: pairing.code });
}

export async function requestPairingCode(): Promise<PairingCodeResult> {
  let response: Response;
  try {
    response = await fetch(PAIRING_CODES_PATH, { method: "POST" });
  } catch {
    return { kind: "unreachable" };
  }
  if (response.status === 403) return { kind: "refused" };
  if (!response.ok) return { kind: "error", status: response.status };
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { kind: "error", status: response.status };
  }
  if (!isPairingCode(body)) return { kind: "error", status: response.status };
  return {
    kind: "ok",
    pairing: { url: body.url, code: body.code, expires_at: body.expires_at },
  };
}
