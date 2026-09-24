import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import PairPage from "./PairPage";

const toString = vi.hoisted(() => vi.fn(async (text: string) => `<svg data-text='${text}'></svg>`));
vi.mock("qrcode", () => ({ default: { toString } }));

const NOW = Date.parse("2026-09-24T10:00:00Z");

function minted(code: string, ttlSeconds: number) {
  return {
    url: "http://192.168.1.20:8765",
    code,
    expires_at: new Date(Date.now() + ttlSeconds * 1000).toISOString(),
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  // Fake only the countdown's clock and interval: Testing Library's `findBy*` needs real timeouts.
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(NOW);
  toString.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function advance(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
}

it("renders the QR from the backend payload, the URL, the code and a decreasing countdown", async () => {
  const fetchMock = vi.fn(async () => jsonResponse(minted("ABCD-EFGH", 300)));
  vi.stubGlobal("fetch", fetchMock);

  render(<PairPage />);

  const qr = await screen.findByRole("img", { name: "Código QR de emparejamiento" });
  expect(fetchMock).toHaveBeenCalledWith("/api/pair/codes", { method: "POST" });
  expect(toString).toHaveBeenCalledTimes(1);
  expect(JSON.parse(toString.mock.calls[0][0])).toEqual({
    url: "http://192.168.1.20:8765",
    code: "ABCD-EFGH",
  });
  expect(qr.getAttribute("src")).toContain(encodeURIComponent("ABCD-EFGH"));
  expect(screen.getByTestId("pair-url")).toHaveTextContent("http://192.168.1.20:8765");
  expect(screen.getByTestId("pair-code")).toHaveTextContent("ABCD-EFGH");
  expect(screen.getByTestId("pair-countdown")).toHaveTextContent("El código caduca en 5:00");

  await advance(3000);
  expect(screen.getByTestId("pair-countdown")).toHaveTextContent("El código caduca en 4:57");
});

it("replaces the QR on expiry and requests a new code from the regenerate button", async () => {
  const fetchMock = vi
    .fn()
    .mockImplementationOnce(async () => jsonResponse(minted("AAAA-BBBB", 2)))
    .mockImplementationOnce(async () => jsonResponse(minted("CCCC-DDDD", 300)));
  vi.stubGlobal("fetch", fetchMock);

  render(<PairPage />);
  await screen.findByRole("img", { name: "Código QR de emparejamiento" });

  await advance(2000);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(screen.queryByText("AAAA-BBBB")).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("El código ha caducado.");

  fireEvent.click(screen.getByRole("button", { name: "Generar un código nuevo" }));

  expect(await screen.findByText("CCCC-DDDD")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("pair-countdown")).toHaveTextContent("El código caduca en 5:00");
});

it("explains in Spanish that codes are only minted on the PC itself when refused", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "no" }, 403)));

  render(<PairPage />);

  expect(await screen.findByRole("alert")).toHaveTextContent(
    /Abre esta página en el propio PC donde se ejecuta Student Assistant/,
  );
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

it("explains in Spanish when the backend is unreachable", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));

  render(<PairPage />);

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo conectar con el servidor.");
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

it("explains in Spanish when the backend answers with an error", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "boom" }, 500)));

  render(<PairPage />);

  expect(await screen.findByRole("alert")).toHaveTextContent("El servidor respondió con un error");
});

it("never stores anything in browser storage", async () => {
  const setItem = vi.spyOn(Storage.prototype, "setItem");
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(minted("ABCD-EFGH", 300))));

  render(<PairPage />);
  await screen.findByText("ABCD-EFGH");

  expect(setItem).not.toHaveBeenCalled();
  setItem.mockRestore();
});
