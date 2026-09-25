import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import BookTitleForm from "./BookTitleForm";

afterEach(() => {
  vi.unstubAllGlobals();
});

const BOOK = "/api/subjects/historia/topics/revolucion/book";

function book(title: string | null) {
  return jsonResponse({ subject_id: "historia", topic_id: "revolucion", title });
}

function renderForm() {
  render(<BookTitleForm subjectId="historia" topicId="revolucion" />);
}

function saveTitle(value: string) {
  fireEvent.change(screen.getByLabelText("Título del libro"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
}

it("shows the topic's book title", async () => {
  stubApi({ [BOOK]: book("Historia del mundo contemporáneo") });
  renderForm();
  expect(await screen.findByText("Libro «Historia del mundo contemporáneo»")).toBeInTheDocument();
  expect(screen.getByLabelText("Título del libro")).toHaveValue("Historia del mundo contemporáneo");
  expect(screen.getByLabelText("Título del libro")).toHaveAttribute("maxLength", "200");
});

it("shows a placeholder when the topic has no book", async () => {
  stubApi({ [BOOK]: book(null) });
  renderForm();
  expect(await screen.findByText("Este tema aún no tiene libro de texto.")).toBeInTheDocument();
  expect(screen.getByLabelText("Título del libro")).toHaveValue("");
});

it("saves a new title and shows the one the backend stored", async () => {
  const fetchMock = stubApi({ [BOOK]: book(null), [`PUT ${BOOK}`]: book("Historia 1º Bachillerato") });
  renderForm();
  await screen.findByText("Este tema aún no tiene libro de texto.");

  saveTitle("Historia   1º Bachillerato");

  expect(await screen.findByRole("status")).toHaveTextContent("Título del libro guardado.");
  expect(screen.getByText("Libro «Historia 1º Bachillerato»")).toBeInTheDocument();
  expect(screen.getByLabelText("Título del libro")).toHaveValue("Historia 1º Bachillerato");
  const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
  expect(JSON.parse(put?.[1]?.body as string)).toEqual({ title: "Historia   1º Bachillerato" });
});

it("shows a 422's Spanish detail and keeps the previous title", async () => {
  stubApi({
    [BOOK]: book("Historia del mundo contemporáneo"),
    [`PUT ${BOOK}`]: jsonResponse({ detail: "El título parece contener una clave o un token y no se ha guardado." }, 422),
  });
  renderForm();
  await screen.findByText("Libro «Historia del mundo contemporáneo»");

  saveTitle("sk-ant-api03-abcdefghijklmnopqrstuvwxyz");

  expect(await screen.findByRole("status")).toHaveTextContent(
    "El título parece contener una clave o un token y no se ha guardado.",
  );
  expect(screen.getByText("Libro «Historia del mundo contemporáneo»")).toBeInTheDocument();
});

it("shows a generic message for any other failure", async () => {
  stubApi({
    [BOOK]: book(null),
    [`PUT ${BOOK}`]: jsonResponse({ detail: "No se puede abrir la bóveda." }, 503),
  });
  renderForm();
  await screen.findByText("Este tema aún no tiene libro de texto.");

  saveTitle("Historia");

  expect(await screen.findByRole("status")).toHaveTextContent(
    "No se ha podido guardar el título del libro. Inténtalo de nuevo.",
  );
  expect(screen.getByText("Este tema aún no tiene libro de texto.")).toBeInTheDocument();
});

it("says so when the title cannot be read", async () => {
  stubApi({ [BOOK]: new Error("down") });
  renderForm();
  expect(await screen.findByText("No se ha podido leer el título del libro.")).toBeInTheDocument();
});
