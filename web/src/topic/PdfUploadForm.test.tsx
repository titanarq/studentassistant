import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PdfUploadForm from "./PdfUploadForm";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const IMPORTED = {
  subject_id: "historia",
  topic_id: "revolucion-industrial",
  source_id: "sources/pdf/page-001.pdf",
  vault_id: "subjects/historia/topics/revolucion-industrial/sources/pdf/page-001.pdf",
  original_name: "Tema 4.pdf",
  original_page_count: 120,
  first_page: 82,
  last_page: 94,
  page_count: 13,
  pages_without_text: [90, 91],
};

function renderForm() {
  render(<PdfUploadForm subjectId="historia" topicId="revolucion-industrial" />);
}

function pickFile(name = "Tema 4.pdf") {
  const file = new File(["%PDF-1.7"], name, { type: "application/pdf" });
  fireEvent.change(screen.getByLabelText("Archivo PDF"), { target: { files: [file] } });
  return file;
}

it("uploads the picked PDF with the typed range and shows what was kept", async () => {
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) =>
    jsonResponse(IMPORTED, 201),
  );
  vi.stubGlobal("fetch", fetchMock);
  renderForm();

  const file = pickFile();
  fireEvent.change(screen.getByLabelText("Páginas (opcional)"), {
    target: { value: " páginas 82 a 94 " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));

  const status = await screen.findByRole("status");
  expect(status).toHaveTextContent("PDF «Tema 4.pdf» añadido: las páginas 82-94 de 120.");
  expect(status).toHaveTextContent("Sin texto extraíble (¿escaneadas?): páginas 90, 91.");
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/subjects/historia/topics/revolucion-industrial/sources/pdf");
  expect(init.method).toBe("POST");
  const form = init.body as FormData;
  expect((form.get("file") as File).name).toBe(file.name);
  expect(form.get("pages")).toBe("páginas 82 a 94");
});

it("sends no pages part when the range is left empty", async () => {
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) =>
    jsonResponse({ ...IMPORTED, first_page: 1, last_page: 1, pages_without_text: [] }, 201),
  );
  vi.stubGlobal("fetch", fetchMock);
  renderForm();

  pickFile();
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));

  expect(await screen.findByRole("status")).toHaveTextContent("la página 1 de 120.");
  expect((fetchMock.mock.calls[0][1].body as FormData).has("pages")).toBe(false);
});

it.each([
  [413, "El rango 1-200 tiene 200 páginas y el máximo por importación es 100: elige un rango más corto."],
  [422, "«Tema 4.pdf» está protegido con contraseña: quítasela antes de importarlo."],
])("shows the backend's refusal (%i) as it comes", async (status, detail) => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail }, status)));
  renderForm();

  pickFile();
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(`No se ha añadido el PDF: ${detail}`);
});

it("explains an error without a detail and an unreachable backend", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("oops", { status: 500 })));
  renderForm();
  pickFile();
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "el servidor respondió con un error (500)",
  );

  vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("down"))));
  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "no se pudo conectar con el servidor",
  );
});

it("asks for a file before uploading anything", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  renderForm();

  fireEvent.click(screen.getByRole("button", { name: "Añadir PDF" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Elige primero un PDF.");
  expect(fetchMock).not.toHaveBeenCalled();
});
