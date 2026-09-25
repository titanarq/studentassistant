import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import WebPageForm from "./WebPageForm";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const ADDED = {
  source_id: "sources/web/001-la-bastilla.md",
  vault_id: "subjects/historia/topics/revolucion/sources/web/001-la-bastilla.md",
  title: "La Bastilla",
  url: "https://historia.example.edu/bastilla",
  already_kept: false,
};

function type(value: string) {
  fireEvent.change(screen.getByLabelText("Dirección de la página"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar como fuente" }));
}

it("stores the pasted page and tells the topic page", async () => {
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => jsonResponse(ADDED, 201));
  vi.stubGlobal("fetch", fetchMock);
  const onAdded = vi.fn();
  render(<WebPageForm subjectId="historia" topicId="revolucion" onAdded={onAdded} />);

  type("  https://historia.example.edu/bastilla ");

  const status = await screen.findByRole("status");
  expect(status).toHaveTextContent("Página «La Bastilla» guardada como fuente externa (sources/web/001-la-bastilla.md).");
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/subjects/historia/topics/revolucion/web-pages");
  expect(init.method).toBe("POST");
  expect(JSON.parse(init.body as string)).toEqual({ url: "https://historia.example.edu/bastilla", via: "url" });
  expect(onAdded).toHaveBeenCalledWith(ADDED);
  expect(screen.getByLabelText("Dirección de la página")).toHaveValue("");
});

it("says so when the topic already had the page", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ...ADDED, already_kept: true })));
  const onAdded = vi.fn();
  render(<WebPageForm subjectId="historia" topicId="revolucion" onAdded={onAdded} />);
  type(ADDED.url);
  expect(await screen.findByRole("status")).toHaveTextContent("Esa página ya era una fuente del tema: «La Bastilla».");
  expect(onAdded).not.toHaveBeenCalled();
});

it("refuses an address that is not a web page without calling the server", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  render(<WebPageForm subjectId="historia" topicId="revolucion" />);
  type("bastilla.example");
  expect(await screen.findByRole("status")).toHaveTextContent("empieza por http:// o https://");
  expect(fetchMock).not.toHaveBeenCalled();
});

it("shows the server's Spanish refusal", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse({ detail: "La página es un PDF: descárgalo e impórtalo como PDF." }, 422)),
  );
  render(<WebPageForm subjectId="historia" topicId="revolucion" />);
  type("https://example.org/tema.pdf");
  expect(await screen.findByRole("status")).toHaveTextContent(
    "No se ha guardado la página: La página es un PDF: descárgalo e impórtalo como PDF.",
  );
});
