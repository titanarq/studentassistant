import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import StyleGuidePage from "./StyleGuidePage";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const GUIDE = "/api/subjects/historia/style-guide";
const SUBJECTS = jsonResponse({ subjects: [{ subject_id: "historia", name: "Historia" }] });
const TABLES = "Usa tablas para comparar conceptos.";
const DATES = "Pon las fechas en negrita.";

function guide(rules: string[], commit: string | null = null) {
  return jsonResponse({ subject: "historia", rules, added: [], commit });
}

function putBodies(fetchMock: ReturnType<typeof stubApi>): string[][] {
  return fetchMock.mock.calls
    .filter((call) => (call[1] as RequestInit | undefined)?.method === "PUT")
    .map((call) => (JSON.parse((call[1] as RequestInit).body as string) as { rules: string[] }).rules);
}

/** The guide holds `rules`; a PUT answers with the list it was sent. */
function stubGuide(rules: string[]) {
  let sent: string[] = [];
  const fetchMock = stubApi({
    "/api/subjects": SUBJECTS,
    [GUIDE]: guide(rules),
    [`PUT ${GUIDE}`]: () => guide(sent, "c1"),
  });
  const route = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
    if (init?.method === "PUT") sent = (JSON.parse(init.body as string) as { rules: string[] }).rules;
    return route(input, init);
  });
  return fetchMock;
}

function rulesList() {
  return screen.getByRole("list", { name: "Reglas de la guía de estilo" });
}

it("lists the subject's rules under its name", async () => {
  stubGuide([TABLES, DATES]);
  render(<StyleGuidePage subjectId="historia" />);

  expect(await screen.findByRole("heading", { name: "Guía de estilo de Historia" })).toBeInTheDocument();
  const items = within(await screen.findByRole("list", { name: "Reglas de la guía de estilo" })).getAllByRole("listitem");
  expect(items.map((item) => item.querySelector(".style-guide-rule")?.textContent)).toEqual([TABLES, DATES]);
  expect(screen.getByRole("link", { name: "← Mesa de estudio" })).toHaveAttribute("href", "/");
});

it("edits a rule and writes the whole list", async () => {
  const fetchMock = stubGuide([TABLES, DATES]);
  render(<StyleGuidePage subjectId="historia" />);

  fireEvent.click(await screen.findByRole("button", { name: `Editar la regla: ${DATES}` }));
  const input = screen.getByLabelText("Regla 2");
  expect(input).toHaveValue(DATES);
  fireEvent.change(input, { target: { value: "Pon las fechas y los nombres en negrita." } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar" }));

  expect(await screen.findByText("Regla modificada.")).toBeInTheDocument();
  expect(putBodies(fetchMock)).toEqual([[TABLES, "Pon las fechas y los nombres en negrita."]]);
  expect(within(rulesList()).getByText("Pon las fechas y los nombres en negrita.")).toBeInTheDocument();
  expect(screen.queryByLabelText("Regla 2")).not.toBeInTheDocument();
});

it("cancels an edit without writing", async () => {
  const fetchMock = stubGuide([TABLES]);
  render(<StyleGuidePage subjectId="historia" />);

  fireEvent.click(await screen.findByRole("button", { name: `Editar la regla: ${TABLES}` }));
  fireEvent.change(screen.getByLabelText("Regla 1"), { target: { value: "Otra cosa." } });
  fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));

  expect(within(rulesList()).getByText(TABLES)).toBeInTheDocument();
  expect(putBodies(fetchMock)).toEqual([]);
});

it("deletes a rule, down to an empty guide", async () => {
  const fetchMock = stubGuide([TABLES]);
  render(<StyleGuidePage subjectId="historia" />);

  fireEvent.click(await screen.findByRole("button", { name: `Borrar la regla: ${TABLES}` }));

  expect(await screen.findByText(`Regla borrada: «${TABLES}».`)).toBeInTheDocument();
  expect(putBodies(fetchMock)).toEqual([[]]);
  expect(screen.getByText(/Todavía no hay reglas/)).toBeInTheDocument();
});

it("adds a rule and refuses a repeated one without writing", async () => {
  const fetchMock = stubGuide([TABLES]);
  render(<StyleGuidePage subjectId="historia" />);

  const input = await screen.findByLabelText("Nueva regla");
  const add = screen.getByRole("button", { name: "Añadir" });
  expect(add).toBeDisabled();
  fireEvent.change(input, { target: { value: DATES } });
  fireEvent.click(add);

  expect(await screen.findByText("Regla añadida.")).toBeInTheDocument();
  expect(input).toHaveValue("");
  expect(putBodies(fetchMock)).toEqual([[TABLES, DATES]]);

  fireEvent.change(input, { target: { value: "usa tablas para  comparar conceptos." } });
  fireEvent.click(add);
  expect(await screen.findByRole("alert")).toHaveTextContent("Esa regla ya está en la guía de estilo.");
  expect(putBodies(fetchMock)).toHaveLength(1);
});

it("shows the backend's refusal and keeps the list", async () => {
  stubApi({
    "/api/subjects": SUBJECTS,
    [GUIDE]: guide([TABLES]),
    [`PUT ${GUIDE}`]: jsonResponse({ detail: "Una regla no puede tener más de 300 caracteres." }, 422),
  });
  render(<StyleGuidePage subjectId="historia" />);

  fireEvent.change(await screen.findByLabelText("Nueva regla"), { target: { value: DATES } });
  fireEvent.click(screen.getByRole("button", { name: "Añadir" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se pudo guardar la guía de estilo: Una regla no puede tener más de 300 caracteres.",
  );
  expect(within(rulesList()).getAllByRole("listitem")).toHaveLength(1);
  expect(screen.getByLabelText("Nueva regla")).toHaveValue(DATES);
});

it("says so for an unknown subject and shows no form", async () => {
  stubApi({
    "/api/subjects": SUBJECTS,
    [GUIDE]: jsonResponse({ detail: "No existe esa asignatura en la bóveda." }, 404),
  });
  render(<StyleGuidePage subjectId="historia" />);

  expect(await screen.findByRole("alert")).toHaveTextContent("No existe esa asignatura en la bóveda.");
  expect(screen.queryByLabelText("Nueva regla")).not.toBeInTheDocument();
});
