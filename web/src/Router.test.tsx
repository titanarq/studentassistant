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
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron cargar las dudas");
});
