import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, sseEvent, streamResponse, stubApi } from "../test/mockApi";
import EditorChat from "./EditorChat";
import { history, revision, turn } from "./testChat";
import { useEditorChat } from "./useEditorChat";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const CHAT = "/api/subjects/historia/topics/revolucion-industrial/notes/chat";
const RULES = "/api/subjects/historia/style-guide/rules";
const TABLES = "Usa tablas para comparar conceptos.";
const EXAMPLES = "Pon un ejemplo en cada sección.";

function Harness() {
  const chat = useEditorChat("historia", "revolucion-industrial", () => {});
  return <EditorChat chat={chat} />;
}

function guide(rules: string[], added: string[]) {
  return jsonResponse({ subject: "historia", rules, added, commit: added.length > 0 ? "c1" : null });
}

function rulesGroups() {
  return screen.queryAllByRole("group", { name: "Propuesta para la guía de estilo" });
}

it("shows the rules a history turn proposes and saves one for the whole subject", async () => {
  let respond!: (response: Response) => void;
  const fetchMock = stubApi({
    [CHAT]: jsonResponse(history([turn({ proposed_style_rules: [TABLES, EXAMPLES] })])),
    [`POST ${RULES}`]: () => new Promise<Response>((resolve) => (respond = resolve)),
  });
  render(<Harness />);

  const group = (await screen.findAllByRole("group", { name: "Propuesta para la guía de estilo" }))[0];
  expect(within(group).getByText(`«${TABLES}»`)).toBeInTheDocument();
  fireEvent.click(within(group).getByRole("button", { name: `Guardar para toda la asignatura: ${TABLES}` }));

  expect(await within(group).findByRole("button", { name: `Guardar para toda la asignatura: ${EXAMPLES}` })).toBeDisabled();
  const post = fetchMock.mock.calls.find((call) => call[0] === RULES);
  expect(JSON.parse((post?.[1] as RequestInit).body as string)).toEqual({ rules: [TABLES] });

  await act(async () => respond(guide([TABLES], [TABLES])));
  expect(await screen.findByText(`Guardado en la guía de estilo de la asignatura: «${TABLES}».`)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Ver la guía de estilo" })).toHaveAttribute("href", "/subjects/historia/style-guide");
  expect(screen.queryByText(`«${TABLES}»`)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: `Guardar para toda la asignatura: ${EXAMPLES}` })).toBeEnabled();
});

it("shows the rules of a live turn's result, and every copy leaves once saved", async () => {
  const stream = streamResponse();
  stubApi({
    [CHAT]: jsonResponse(history([turn({ proposed_style_rules: [TABLES] })])),
    [`POST ${CHAT}`]: () => stream.response,
    [`POST ${RULES}`]: guide([TABLES], [TABLES]),
  });
  render(<Harness />);
  await screen.findByText(`«${TABLES}»`);

  const input = screen.getByLabelText("Mensaje para el editor");
  fireEvent.change(input, { target: { value: "Compara con una tabla" } });
  fireEvent.keyDown(input, { key: "Enter" });
  await act(async () => {
    stream.push(sseEvent("result", revision({ proposed_style_rules: [TABLES] })));
    stream.close();
  });
  await waitFor(() => expect(rulesGroups()).toHaveLength(2));

  fireEvent.click(screen.getAllByRole("button", { name: `Guardar para toda la asignatura: ${TABLES}` })[1]);
  await waitFor(() => expect(rulesGroups()).toHaveLength(0));
});

it("says when the guide already had the rule and when saving failed", async () => {
  stubApi({
    [CHAT]: jsonResponse(history([turn({ proposed_style_rules: [TABLES] }), turn({ proposed_style_rules: [EXAMPLES] })])),
    [`POST ${RULES}`]: jsonResponse({ detail: "No existe esa asignatura en la bóveda." }, 404),
  });
  render(<Harness />);

  fireEvent.click(await screen.findByRole("button", { name: `Guardar para toda la asignatura: ${TABLES}` }));
  expect(
    await screen.findByText("No se pudo guardar la regla: No existe esa asignatura en la bóveda.", { exact: false }),
  ).toBeInTheDocument();
  expect(screen.getByText(`«${TABLES}»`)).toBeInTheDocument();

  stubApi({
    [CHAT]: jsonResponse(history()),
    [`POST ${RULES}`]: guide([EXAMPLES], []),
  });
  fireEvent.click(screen.getByRole("button", { name: `Guardar para toda la asignatura: ${EXAMPLES}` }));
  expect(
    await screen.findByText(`La guía de estilo de la asignatura ya tenía esa regla: «${EXAMPLES}».`, { exact: false }),
  ).toBeInTheDocument();
  expect(screen.queryByText(`«${EXAMPLES}»`)).not.toBeInTheDocument();
});
