/**
 * Session health on the capture page (#262): the lines a summary gives, one ask against a mocked
 * `fetch`, and the polling hook on fake timers -- nothing while healthy, one Spanish line per
 * problem, and no more asking once the session ends or the backend forgets it.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  fetchSessionHealth,
  HEALTH_POLL_MS,
  healthLines,
  type SessionHealth,
  sessionHealthPath,
  useSessionHealth,
} from "./sessionHealth";

const SESSION_ID = "20260925-101500";
const PATH = `/api/sessions/${SESSION_ID}/health`;

const HEALTHY: SessionHealth = {
  session_id: SESSION_ID,
  ok: true,
  observer: { count: 0, message: null },
  observer_paused: false,
  observer_paused_message: null,
  transcription: { count: 0, message: null },
  push: { count: 0, message: null },
};

const FAILING: SessionHealth = {
  ...HEALTHY,
  ok: false,
  observer: { count: 3, message: "Claude no responde (sin conexión o saturado)" },
  observer_paused: true,
  observer_paused_message: "se ha alcanzado el límite de gasto de la sesión",
  transcription: { count: 1, message: "Claude ha rechazado la página" },
  push: { count: 2, message: "no hay conexión con GitHub" },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let answers: Array<() => Response>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  answers = [];
  fetchMock = vi.fn(async (_path: string) => {
    const next = answers.shift();
    return next === undefined ? jsonResponse(HEALTHY) : next();
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("healthLines", () => {
  it("says nothing for a healthy session or before any answer", () => {
    expect(healthLines(HEALTHY)).toEqual([]);
    expect(healthLines(null)).toEqual([]);
  });

  it("gives one Spanish line per problem", () => {
    expect(healthLines(FAILING)).toEqual([
      "El observador ha fallado 3 veces: Claude no responde (sin conexión o saturado).",
      "El observador está en pausa: se ha alcanzado el límite de gasto de la sesión.",
      "No se han podido transcribir 1 página: Claude ha rechazado la página.",
      "La bóveda no se ha podido subir a GitHub (2 veces): no hay conexión con GitHub.",
    ]);
  });

  it("uses the singular for one observer failure", () => {
    const once = { ...HEALTHY, ok: false, observer: { count: 1, message: "Claude ha fallado" } };
    expect(healthLines(once)).toEqual(["El observador ha fallado 1 vez: Claude ha fallado."]);
  });
});

describe("fetchSessionHealth", () => {
  it("asks the session's health route and decodes the summary", async () => {
    answers.push(() => jsonResponse(FAILING));
    expect(await fetchSessionHealth(SESSION_ID)).toEqual(FAILING);
    expect(fetchMock).toHaveBeenCalledWith(PATH, { method: "GET" });
    expect(sessionHealthPath("a b")).toBe("/api/sessions/a%20b/health");
  });

  it("answers gone for an unknown session and null for anything unusable", async () => {
    answers.push(() => jsonResponse({ detail: "no existe la sesión" }, 404));
    answers.push(() => jsonResponse({ detail: "boom" }, 500));
    answers.push(() => jsonResponse({ ok: "yes" }));
    answers.push(() => {
      throw new TypeError("offline");
    });
    expect(await fetchSessionHealth(SESSION_ID)).toBe("gone");
    expect(await fetchSessionHealth(SESSION_ID)).toBeNull();
    expect(await fetchSessionHealth(SESSION_ID)).toBeNull();
    expect(await fetchSessionHealth(SESSION_ID)).toBeNull();
  });
});

describe("useSessionHealth", () => {
  async function tick(ms: number): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("shows nothing while the session is healthy, asking once per interval", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useSessionHealth(SESSION_ID, true));
    expect(fetchMock).not.toHaveBeenCalled();

    await tick(HEALTH_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current).toEqual([]);

    await tick(HEALTH_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current).toEqual([]);
  });

  it("shows the lines of a failing session and clears them once it recovers", async () => {
    vi.useFakeTimers();
    answers.push(() => jsonResponse(FAILING));
    const { result } = renderHook(() => useSessionHealth(SESSION_ID, true));

    await tick(HEALTH_POLL_MS);
    expect(result.current).toHaveLength(4);
    expect(result.current[0]).toBe(
      "El observador ha fallado 3 veces: Claude no responde (sin conexión o saturado).",
    );

    await tick(HEALTH_POLL_MS); // the next answer is healthy
    expect(result.current).toEqual([]);
  });

  it("keeps the last lines when one ask fails", async () => {
    vi.useFakeTimers();
    answers.push(() => jsonResponse(FAILING));
    answers.push(() => jsonResponse({ detail: "boom" }, 500));
    const { result } = renderHook(() => useSessionHealth(SESSION_ID, true));

    await tick(HEALTH_POLL_MS);
    await tick(HEALTH_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current).toHaveLength(4);
  });

  it("stops asking when the session ends", async () => {
    vi.useFakeTimers();
    answers.push(() => jsonResponse(FAILING));
    const { result, rerender } = renderHook(
      ({ active }) => useSessionHealth(SESSION_ID, active),
      { initialProps: { active: true } },
    );
    await tick(HEALTH_POLL_MS);
    expect(result.current).toHaveLength(4);

    rerender({ active: false });
    expect(result.current).toEqual([]);
    await tick(HEALTH_POLL_MS * 5);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops asking when the backend no longer knows the session", async () => {
    vi.useFakeTimers();
    answers.push(() => jsonResponse({ detail: "no existe la sesión" }, 404));
    renderHook(() => useSessionHealth(SESSION_ID, true));

    await tick(HEALTH_POLL_MS * 5);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops asking on unmount", async () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() => useSessionHealth(SESSION_ID, true));
    unmount();
    await tick(HEALTH_POLL_MS * 3);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
