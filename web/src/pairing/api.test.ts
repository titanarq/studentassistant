import { afterEach, expect, it, vi } from "vitest";
import { PAIRING_CODES_PATH, qrPayload, requestPairingCode } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

const minted = {
  url: "http://192.168.1.20:8765",
  code: "ABCD-EFGH",
  expires_at: "2026-09-24T10:05:00Z",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

it("returns the pairing code on success", async () => {
  const fetchMock = vi.fn(async () => jsonResponse(minted));
  vi.stubGlobal("fetch", fetchMock);

  expect(await requestPairingCode()).toEqual({ kind: "ok", pairing: minted });
  expect(fetchMock).toHaveBeenCalledWith(PAIRING_CODES_PATH, { method: "POST" });
});

it("maps 403 to refused (not opened on the PC itself)", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "nope" }, 403)));

  expect(await requestPairingCode()).toEqual({ kind: "refused" });
});

it("maps any other non-2xx response to an error with its status", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "boom" }, 500)));

  expect(await requestPairingCode()).toEqual({ kind: "error", status: 500 });
});

it("maps a 2xx body that is not a pairing code to an error", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ url: "x" })));

  expect(await requestPairingCode()).toEqual({ kind: "error", status: 200 });
});

it("maps a network error to unreachable", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));

  expect(await requestPairingCode()).toEqual({ kind: "unreachable" });
});

it("encodes exactly url and code in the QR payload", () => {
  expect(JSON.parse(qrPayload(minted))).toEqual({ url: minted.url, code: minted.code });
});
