import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import Router from "./Router";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch() {
  const fetchMock = vi.fn(async () => new Response("{}", { status: 503 }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

it.each(["/pair", "/pair/"])("renders the pairing page at %s", (pathname) => {
  const fetchMock = stubFetch();

  render(<Router pathname={pathname} />);

  expect(screen.getByRole("heading", { name: "Emparejar un dispositivo" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/pair/codes", { method: "POST" });
});

it("renders the study desk at /", () => {
  stubFetch();

  render(<Router pathname="/" />);

  expect(screen.getByRole("heading", { name: "Mesa de estudio" })).toBeInTheDocument();
});

it.each(["/subjects/historia/topics/revolucion-industrial", "/subjects/historia/topics/revolucion-industrial/"])(
  "renders the topic page at %s",
  (pathname) => {
    stubFetch();

    render(<Router pathname={pathname} />);

    expect(screen.getByRole("heading", { name: "Tema revolucion-industrial" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Añadir un PDF" })).toBeInTheDocument();
  },
);

it("renders the notes viewer at the topic's /notes path", async () => {
  stubFetch();

  render(<Router pathname="/subjects/historia/topics/revolucion-industrial/notes" />);

  expect(screen.getByRole("link", { name: "← Tema revolucion-industrial" })).toHaveAttribute(
    "href",
    "/subjects/historia/topics/revolucion-industrial",
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron cargar los apuntes");
});

it("renders the pending-doubts panel at the topic's /pending path", async () => {
  stubFetch();

  render(<Router pathname="/subjects/historia/topics/revolucion-industrial/pending" />);

  expect(screen.getByRole("heading", { name: "Dudas pendientes" })).toBeInTheDocument();
  expect(await screen.findByText(/No se pudieron cargar las dudas/)).toBeInTheDocument();
});

it("renders the notes versions page at the topic's /versions path", () => {
  const fetchMock = stubFetch();

  render(<Router pathname="/subjects/historia/topics/revolucion-industrial/versions" />);

  expect(screen.getByRole("heading", { name: "Versiones de los apuntes de revolucion-industrial" })).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/subjects/historia/topics/revolucion-industrial/notes/versions");
});

it("renders the live session view at /live, subscribed to the live stream", () => {
  stubFetch();
  const opened: string[] = [];
  class FakeEventSource {
    onopen = null;
    onerror = null;
    constructor(url: string) {
      opened.push(url);
    }
    addEventListener() {}
    close() {}
  }
  vi.stubGlobal("EventSource", FakeEventSource);

  render(<Router pathname="/live" />);

  expect(screen.getByRole("heading", { name: "Sesión en directo" })).toBeInTheDocument();
  expect(opened).toEqual(["/api/live"]);
});

it("renders the quiz page at <topic>/quiz", () => {
  stubFetch();

  render(<Router pathname="/subjects/historia/topics/revolucion-industrial/quiz" />);

  expect(screen.getByRole("heading", { name: "Quiz de revolucion-industrial" })).toBeInTheDocument();
});
