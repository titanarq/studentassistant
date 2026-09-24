import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import App from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

it("shows the heading and the backend protocol version from /api/health", async () => {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ status: "ok", protocol_version: "1.0", server_time_ms: 1790251200000 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  expect(screen.getByRole("heading", { name: "Mesa de estudio" })).toBeInTheDocument();
  expect(await screen.findByText("Servidor en marcha, protocolo 1.0")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/health");
});

it("shows a Spanish error when the backend is unreachable", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new Error("offline"))));

  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo conectar con el servidor.");
});

it("shows the Spanish error when /api/health is not a protocol v1 health response", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );

  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo conectar con el servidor.");
});
