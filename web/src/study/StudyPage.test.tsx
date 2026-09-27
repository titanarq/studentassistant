import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { NOTES } from "../notes/testNotes";
import { jsonResponse, sseResponse, stubApi } from "../test/mockApi";
import StudyPage, { sectionTitles } from "./StudyPage";
import { parseNotes } from "../notes/markdown";

const BASE = "/api/subjects/historia/topics/revolucion-industrial";
const PAGE = "/subjects/historia/topics/revolucion-industrial";
const BUILT_AT = "2026-09-25T18:00:00Z";

const KINDS = { esquema: "esquema", ejercicios: "examen", examen: "examen", quiz: "quiz", tarjetas: "flashcards" };

/** A `GET .../study` body: each option `listo` unless `states` says otherwise. */
function studyBody(
  states: Record<string, [string, string | null]> = {},
  label: { version: number; current: boolean } | null = { version: 5, current: true },
) {
  return {
    subject: "historia",
    topic: "revolucion-industrial",
    study_version:
      label === null
        ? null
        : { version: label.version, tag: `historia/revolucion-industrial/apuntes-v${label.version}`, marked_at: BUILT_AT },
    study_current: label?.current ?? false,
    options: Object.entries(KINDS).map(([key, kind]) => {
      const [state, reason] = states[key] ?? ["listo", null];
      return { key, kind, state, stale_reason: reason, notes_version: state === "sin_generar" ? null : 2 };
    }),
    created_tag: false,
    ended_session: null,
  };
}

const READY = studyBody();

const MIXED = studyBody({
  quiz: ["desactualizado", "Los apuntes cambiaron en la v3."],
  ejercicios: ["sin_generar", null],
  examen: ["sin_generar", null],
});

function summary(topics: Record<string, unknown>[]) {
  return jsonResponse({ now: "2026-09-26T08:00:00Z", topics, totals: { due: 0, new: 0, topics: topics.length }, warnings: [] });
}

const TOPIC_PRACTICE = {
  subject_id: "historia",
  subject_name: "Historia",
  topic_id: "revolucion-industrial",
  topic_title: "La Revolución Industrial",
  due: 3,
  new: 2,
  next_due: null,
};

const CARD = {
  item: {
    key: "flashcards:c1",
    source: "flashcards",
    prompt: "¿Qué recursos impulsaron la industria?",
    answer: "El carbón y el hierro.",
    question_type: null,
    options: [],
    explanation: "",
    anchors: ["causas"],
  },
  state: null,
};

function practice(items: unknown[] = [CARD]) {
  return jsonResponse({
    queue: items,
    counts: { total: items.length, due: 0, new: items.length, unseen: items.length, learned: 0, suspended: 0 },
    suspended: [],
    next_due: null,
    warnings: [],
  });
}

function quiz(anchors = ["contexto"]) {
  return jsonResponse({
    quiz: {
      title: "Quiz",
      difficulty: "mixed",
      questions: [
        {
          id: "q1",
          type: "multiple_choice",
          difficulty: "easy",
          question: "¿Dónde empezó la Revolución Industrial?",
          options: ["Gran Bretaña", "Francia"],
          answer: "Gran Bretaña",
          explanation: "",
          anchors,
        },
      ],
    },
    built_at: BUILT_AT,
    notes_version: 2,
    warnings: [],
    stale: false,
    stale_reason: null,
  });
}

function question(id: string, number: number, statement: string, anchors: string[], points: number | null = 5) {
  return { id, number, statement, difficulty: "media", points, solution: `Solución de ${id}.`, rubric: [], anchors };
}

const EXAM = jsonResponse({
  exam: {
    title: "La Revolución Industrial",
    instructions: "",
    duration_minutes: 60,
    total_points: 10,
    exercises: [
      question("e1", 1, "Explica el papel del carbón.", ["causas"], null),
      question("e2", 2, "Resume el contexto.", ["contexto"], null),
    ],
    questions: [question("p1", 1, "¿Qué mejoró Watt?", ["maquina-de-vapor"], 10)],
  },
  built_at: BUILT_AT,
  notes_version: 2,
  warnings: [],
  stale: false,
  stale_reason: null,
});

function routes(overrides: Record<string, Response | (() => Response)> = {}) {
  return {
    "/api/subjects/historia/topics": jsonResponse({
      subject_id: "historia",
      topics: [{ topic_id: "revolucion-industrial", subject_id: "historia", name: "La Revolución Industrial" }],
    }),
    [`${BASE}/notes`]: jsonResponse({ subject_id: "historia", topic_id: "revolucion-industrial", text: NOTES, version: 5 }),
    [`${BASE}/study`]: jsonResponse(READY),
    [`${BASE}/generated/files/esquema.md`]: new Response("# Esquema del tema\n\n- Contexto\n- Causas\n"),
    "/api/practice/summary": summary([TOPIC_PRACTICE]),
    [`${BASE}/practice`]: practice(),
    [`${BASE}/quiz`]: quiz(),
    [`${BASE}/quiz/results`]: jsonResponse([]),
    [`${BASE}/exam`]: EXAM,
    [`${BASE}/exam/results`]: jsonResponse([]),
    [`${BASE}/tutor`]: jsonResponse({ subject: "historia", topic: "revolucion-industrial", turns: [] }),
    ...overrides,
  };
}

const scrollTo = vi.fn();

beforeEach(() => {
  scrollTo.mockReset();
  Element.prototype.scrollTo = scrollTo;
});

afterEach(() => {
  vi.unstubAllGlobals();
  // @ts-expect-error jsdom has no Element.scrollTo of its own
  delete Element.prototype.scrollTo;
});

function renderPage(overrides: Record<string, Response | (() => Response)> = {}) {
  const fetchMock = stubApi(routes(overrides));
  const { container } = render(<StudyPage subjectId="historia" topicId="revolucion-industrial" />);
  return { fetchMock, root: container.firstElementChild as HTMLElement };
}

function optionButton(name: RegExp) {
  return screen.getByRole("button", { name });
}

async function options() {
  return within(screen.getByRole("region", { name: "Material de estudio" })).findAllByRole("button");
}

/** The block (the `notes-block` wrapper) of a document heading. */
function block(name: RegExp) {
  const doc = screen.getByRole("region", { name: "Documento" });
  return within(doc).getByRole("heading", { name }).closest(".notes-block") as HTMLElement;
}

it("shows the options, today's reviews, the question chat and the document in read mode", async () => {
  renderPage();

  expect(screen.getByRole("heading", { level: 1, name: "Estudiar" })).toBeInTheDocument();
  const modes = screen.getByRole("navigation", { name: "Modo del tema" });
  expect(within(modes).getByRole("link", { name: "Estudiar" })).toHaveAttribute("aria-current", "page");
  expect(within(modes).getByRole("link", { name: "Construir" })).toHaveAttribute("href", `${PAGE}/workspace`);
  expect(within(modes).getByRole("link", { name: "Construir" })).not.toHaveAttribute("aria-current");

  const doc = screen.getByRole("region", { name: "Documento" });
  expect(await within(doc).findByRole("heading", { name: /Contexto/ })).toBeInTheDocument();
  expect(within(doc).getByRole("link", { name: "Editar en Construir" })).toHaveAttribute("href", `${PAGE}/workspace`);
  expect(within(doc).getByText("v5")).toBeInTheDocument();
  // Read mode: no "¿Por qué?" and nothing to edit.
  expect(within(doc).queryByRole("button", { name: "¿Por qué pusiste esto?" })).toBeNull();
  expect(within(doc).queryByRole("textbox")).toBeNull();

  const reviews = screen.getByRole("region", { name: "Repasos para hoy" });
  expect(await within(reviews).findByText(/3 para repasar · 2 nuevas/)).toBeInTheDocument();
  const chat = screen.getByRole("region", { name: "Preguntas sobre el documento" });
  expect(within(chat).getByRole("textbox", { name: "Tu pregunta" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "La Revolución Industrial" })).toBeInTheDocument();

  const names = (await options()).map((button) => within(button).getByText(/^[A-Z]/, { selector: ".study-option-title" }).textContent);
  expect(names).toEqual(["Esquema", "Ejercicios", "Examen", "Quiz", "Tarjetas de memoria"]);
});

it("names the versión de estudio in the header", async () => {
  renderPage();

  const header = screen.getByRole("banner");
  expect(await within(header).findByText(/^Apuntes v5 ·/)).toHaveTextContent("Apuntes v5 · versión de estudio");
  expect(within(header).queryByText(/Has cambiado los apuntes/)).toBeNull();
});

it("says the notes changed after the versión de estudio, with nothing to do about it", async () => {
  renderPage({ [`${BASE}/study`]: jsonResponse(studyBody({}, { version: 4, current: false })) });

  const header = screen.getByRole("banner");
  expect(await within(header).findByText("Has cambiado los apuntes después de la versión de estudio (v4).")).toBeInTheDocument();
  expect(within(header).getByText(/^Apuntes v4 ·/)).toBeInTheDocument();
  expect(within(header).queryByRole("button", { name: /versión/ })).toBeNull();
});

it("shows no study label when no version was marked yet", async () => {
  renderPage({ [`${BASE}/study`]: jsonResponse(studyBody({}, null)) });

  await options();
  expect(screen.queryByText(/versión de estudio/)).toBeNull();
});

it("says when the next review is when there is nothing to review today", async () => {
  renderPage({ "/api/practice/summary": summary([{ ...TOPIC_PRACTICE, due: 0, new: 0, next_due: "2026-09-28T08:00:00Z" }]) });

  const reviews = screen.getByRole("region", { name: "Repasos para hoy" });
  expect(await within(reviews).findByText(/^Nada que repasar hoy\. Próximo repaso: 28 de septiembre de 2026/)).toBeInTheDocument();
  expect(within(reviews).queryByRole("button")).toBeNull();
});

it("shows each option's state as a text badge: Listo, Desactualizado with its reason, Sin generar", async () => {
  renderPage({ [`${BASE}/study`]: jsonResponse(MIXED) });

  await options();
  expect(within(optionButton(/^Esquema/)).getByText("Listo")).toBeInTheDocument();
  const stale = within(optionButton(/^Quiz/)).getByText("Desactualizado");
  expect(stale).toHaveAttribute("title", "Los apuntes cambiaron en la v3.");
  expect(within(optionButton(/^Ejercicios/)).getByText("Sin generar")).toBeInTheDocument();
  expect(within(optionButton(/^Examen/)).getByText("Sin generar")).toBeInTheDocument();
  expect(within(optionButton(/^Tarjetas de memoria/)).getByText("Listo")).toBeInTheDocument();
});

it("reads the states from GET .../study, not from GET .../generated", async () => {
  const { fetchMock } = renderPage({ [`${BASE}/study`]: jsonResponse(studyBody({ tarjetas: ["desactualizado", null] })) });

  await options();
  // No reason from the backend: the generic one.
  expect(within(optionButton(/^Tarjetas de memoria/)).getByText("Desactualizado")).toHaveAttribute(
    "title",
    "Los apuntes han cambiado desde que se generó.",
  );
  expect(fetchMock).toHaveBeenCalledWith(`${BASE}/study`);
  expect(fetchMock).not.toHaveBeenCalledWith(`${BASE}/generated`);
});

it("says when the state of the material cannot be read", async () => {
  renderPage({ [`${BASE}/study`]: jsonResponse({ detail: "boom" }, 503) });

  expect(await screen.findByText(/No se pudo leer el estado del material/)).toBeInTheDocument();
});

it.each([
  [/^Esquema/, "Esquema", "Esquema del tema"],
  [/^Ejercicios/, "Ejercicios", "Explica el papel del carbón."],
  [/^Examen/, "Examen", "¿Qué mejoró Watt?"],
  [/^Quiz/, "Quiz", "¿Dónde empezó la Revolución Industrial?"],
  [/^Tarjetas de memoria/, "Tarjetas de memoria", "¿Qué recursos impulsaron la industria?"],
])("opens %s in the panel over the document and closes it back to its button", async (name, title, content) => {
  renderPage();
  await options();
  const button = optionButton(name);
  expect(button).toHaveAttribute("aria-expanded", "false");

  fireEvent.click(button);

  const panel = screen.getByRole("region", { name: title });
  expect(button).toHaveAttribute("aria-expanded", "true");
  expect(await within(panel).findByText(content)).toBeInTheDocument();
  // Embedded: no crumbs or page heading of its own.
  expect(within(panel).queryByRole("link", { name: /← Tema/ })).toBeNull();
  expect(within(panel).queryByRole("heading", { level: 1, name: /(de|Practicar) La Revolución Industrial$/ })).toBeNull();
  // Not a modal: the document stays there.
  expect(screen.getByRole("region", { name: "Documento" })).toContainElement(panel);
  expect(screen.getByRole("heading", { name: /Contexto/ })).toBeVisible();

  fireEvent.click(within(panel).getByRole("button", { name: "Cerrar" }));

  expect(screen.queryByRole("region", { name: title })).toBeNull();
  expect(button).toHaveAttribute("aria-expanded", "false");
  expect(button).toHaveFocus();
});

it("closes the panel with Escape", async () => {
  renderPage();
  await options();
  fireEvent.click(optionButton(/^Quiz/));
  const panel = screen.getByRole("region", { name: "Quiz" });

  fireEvent.keyDown(within(panel).getByRole("heading", { name: "Quiz" }), { key: "Escape" });

  expect(screen.queryByRole("region", { name: "Quiz" })).toBeNull();
  expect(optionButton(/^Quiz/)).toHaveFocus();
});

it("opens Tarjetas de memoria from Repasos para hoy, with the ratings and source chips", async () => {
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "Repasar ahora" }));

  const panel = screen.getByRole("region", { name: "Tarjetas de memoria" });
  fireEvent.click(await within(panel).findByRole("button", { name: "Mostrar respuesta" }));
  const ratings = within(panel).getByRole("group", { name: "¿Cómo te ha ido?" });
  expect(within(ratings).getAllByRole("button").map((b) => b.textContent)).toEqual(["Otra vez", "Difícil", "Bien", "Fácil"]);
  expect(within(panel).getByRole("link", { name: "§ 2. Causas" })).toHaveClass("practice-chip");
});

it("says an option is not generated yet and what to ask the chat, with no Generar button", async () => {
  const { fetchMock } = renderPage({ [`${BASE}/study`]: jsonResponse(MIXED) });
  await options();
  fireEvent.click(optionButton(/^Ejercicios/));
  const panel = screen.getByRole("region", { name: "Ejercicios" });

  expect(within(panel).getByText("Todavía no está generado.")).toBeInTheDocument();
  expect(within(panel).getByRole("button", { name: "hazme ejercicios" }).closest("p")).toHaveTextContent(
    "Pídelo en el chat: «hazme ejercicios».",
  );
  expect(within(panel).queryByRole("button", { name: /Generar/ })).toBeNull();
  expect(fetchMock).not.toHaveBeenCalledWith(`${BASE}/exam`);
});

it("shows the reason and the chat hint above a stale option, with no generate form", async () => {
  renderPage({ [`${BASE}/study`]: jsonResponse(MIXED) });
  await options();
  fireEvent.click(optionButton(/^Quiz/));
  const panel = screen.getByRole("region", { name: "Quiz" });

  expect(within(panel).getByRole("note")).toHaveTextContent("Los apuntes cambiaron en la v3.");
  expect(within(panel).getByRole("note")).toHaveTextContent("Pídelo de nuevo en el chat: «hazme un quiz».");
  expect(await within(panel).findByText("¿Dónde empezó la Revolución Industrial?")).toBeInTheDocument();
  expect(within(panel).queryByRole("button", { name: /Generar/ })).toBeNull();
  expect(within(panel).queryByRole("form", { name: "Generar un quiz" })).toBeNull();
});

it("highlights the section of the flashcard shown and scrolls to it", async () => {
  renderPage();
  await screen.findByRole("heading", { name: /Causas/ });
  await options();
  expect(document.querySelector(".notes-focus")).toBeNull();

  fireEvent.click(optionButton(/^Tarjetas de memoria/));
  await screen.findByText("¿Qué recursos impulsaron la industria?");

  await waitFor(() => expect(block(/Causas/)).toHaveClass("notes-focus"));
  // The subsection is part of the section.
  expect(block(/La máquina de vapor/)).toHaveClass("notes-focus");
  expect(block(/Contexto/)).not.toHaveClass("notes-focus");
  // The document (not the page) scrolls.
  expect(scrollTo).toHaveBeenCalled();
  expect(scrollTo.mock.contexts.at(-1)).toHaveClass("study-document");

  fireEvent.click(within(screen.getByRole("region", { name: "Tarjetas de memoria" })).getByRole("button", { name: "Cerrar" }));
  expect(document.querySelector(".notes-focus")).toBeNull();
});

it("highlights the section of the quiz question the student is on", async () => {
  renderPage();
  await screen.findByRole("heading", { name: /Contexto/ });
  await options();
  fireEvent.click(optionButton(/^Quiz/));
  const panel = screen.getByRole("region", { name: "Quiz" });

  fireEvent.focus(await within(panel).findByRole("radio", { name: "Gran Bretaña" }));

  expect(block(/Contexto/)).toHaveClass("notes-focus");
  expect(block(/Causas/)).not.toHaveClass("notes-focus");
});

it("ignores an anchor the notes lack", async () => {
  renderPage({ [`${BASE}/quiz`]: quiz(["no-existe"]) });
  await screen.findByRole("heading", { name: /Contexto/ });
  await options();
  fireEvent.click(optionButton(/^Quiz/));

  fireEvent.focus(await screen.findByRole("radio", { name: "Gran Bretaña" }));

  expect(document.querySelector(".notes-focus")).toBeNull();
  expect(scrollTo).not.toHaveBeenCalled();
});

it("moves through the exercises one at a time, each highlighting its section", async () => {
  renderPage();
  await screen.findByRole("heading", { name: /Contexto/ });
  await options();
  fireEvent.click(optionButton(/^Ejercicios/));
  const panel = screen.getByRole("region", { name: "Ejercicios" });

  expect(await within(panel).findByText("Ejercicio 1 de 2")).toBeInTheDocument();
  expect(within(panel).queryByText("Solución de e1.")).toBeNull();
  fireEvent.click(within(panel).getByRole("button", { name: "Ver solución" }));
  expect(within(panel).getByText("Solución de e1.")).toBeInTheDocument();
  expect(block(/Causas/)).toHaveClass("notes-focus");

  fireEvent.click(within(panel).getByRole("button", { name: "Siguiente" }));

  expect(within(panel).getByText("Resume el contexto.")).toBeInTheDocument();
  expect(within(panel).queryByText("Solución de e2.")).toBeNull();
  expect(block(/Contexto/)).toHaveClass("notes-focus");
  expect(block(/Causas/)).not.toHaveClass("notes-focus");
});

it("opens a provenance footnote's source over the document and gives the focus back", async () => {
  renderPage();
  const doc = screen.getByRole("region", { name: "Documento" });
  const reference = (await within(doc).findAllByRole("link", { name: "Fuente: Apuntes, página 1" }))[0];

  fireEvent.click(reference);

  const source = screen.getByRole("dialog", { name: "Apuntes, página 1" });
  expect(doc).toContainElement(source);
  fireEvent.click(within(source).getByRole("button", { name: "Cerrar" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(reference).toHaveFocus();
});

it("says there are no notes yet, linking to Construir", async () => {
  renderPage({ [`${BASE}/notes`]: jsonResponse({ detail: "No hay apuntes." }, 404) });

  const empty = await screen.findByText(/Todavía no hay apuntes/);
  expect(empty).toHaveTextContent("Todavía no hay apuntes: constrúyelos en Construir.");
  expect(within(empty).getByRole("link", { name: "Construir" })).toHaveAttribute("href", `${PAGE}/workspace`);
  expect(screen.getByRole("link", { name: "Editar en Construir" })).toBeInTheDocument();
});

it("switches between Estudiar and Documento in one column, and an option shows the document", async () => {
  const { root } = renderPage();
  const switcher = screen.getByRole("group", { name: "Qué mostrar" });
  const studyView = within(switcher).getByRole("button", { name: "Estudiar" });
  const documentView = within(switcher).getByRole("button", { name: "Documento" });
  expect(root).toHaveAttribute("data-view", "study");
  expect(studyView).toHaveAttribute("aria-pressed", "true");

  fireEvent.click(documentView);
  expect(root).toHaveAttribute("data-view", "document");
  expect(documentView).toHaveAttribute("aria-pressed", "true");

  fireEvent.click(studyView);
  await options();
  fireEvent.click(optionButton(/^Quiz/));
  expect(root).toHaveAttribute("data-view", "document");

  await act(async () => {
    fireEvent.click(within(screen.getByRole("region", { name: "Quiz" })).getByRole("button", { name: "Cerrar" }));
  });
  expect(root).toHaveAttribute("data-view", "study");
});

it("names each section by its heading text", () => {
  const titles = sectionTitles(parseNotes(NOTES));
  expect(titles.get("causas")).toBe("2. Causas");
  expect(titles.get("maquina-de-vapor")).toBe("2.1. La máquina de vapor");
});

// ---- The question chat's citation chips (#336) ----

function written(reply: string, overrides: Record<string, unknown> = {}) {
  return {
    time: "2026-09-26T19:04:00Z",
    style: "written",
    question: "¿Qué causas tuvo?",
    reply,
    refs: [{ label: "p2", kind: "notes_page", text: "Apuntes, página 2", source_id: null, path: null }],
    sections: [{ anchor: "causas", title: "2. Causas" }],
    warning: null,
    ...overrides,
  };
}

function chatRegion() {
  return screen.getByRole("region", { name: "Preguntas sobre el documento" });
}

it("a section chip of an answer scrolls the document to that section and highlights it", async () => {
  renderPage({
    [`${BASE}/tutor`]: jsonResponse({ turns: [written("El carbón y el hierro [§causas].[^p2]")] }),
  });
  await screen.findByRole("heading", { name: /Causas/ });
  const chip = await within(chatRegion()).findByRole("button", { name: "Ir a la sección 2. Causas" });
  expect(chip).toHaveTextContent("§ 2. Causas");
  expect(document.querySelector(".notes-focus")).toBeNull();

  fireEvent.click(chip);

  await waitFor(() => expect(block(/Causas/)).toHaveClass("notes-focus"));
  expect(block(/Contexto/)).not.toHaveClass("notes-focus");
  expect(scrollTo).toHaveBeenCalled();
  expect(scrollTo.mock.contexts.at(-1)).toHaveClass("study-document");
});

it("a source chip highlights the blocks citing it and opens its source over the document", async () => {
  renderPage({
    [`${BASE}/tutor`]: jsonResponse({ turns: [written("El carbón y el hierro [§causas].[^p2]")] }),
  });
  const doc = screen.getByRole("region", { name: "Documento" });
  await within(doc).findByRole("heading", { name: /Causas/ });
  const chip = await within(chatRegion()).findByRole("button", { name: "Ver la fuente p2" });
  expect(chip).toHaveTextContent("p2");

  fireEvent.click(chip);

  const source = screen.getByRole("dialog", { name: "Apuntes, página 2" });
  expect(doc).toContainElement(source);
  // The list citing [^p2] is highlighted, the rest is not.
  const cited = within(doc).getByText(/Disponibilidad de carbón y hierro/).closest(".notes-block");
  expect(cited).toHaveClass("notes-focus");
  expect(block(/Contexto/)).not.toHaveClass("notes-focus");
  expect(scrollTo).toHaveBeenCalled();

  fireEvent.click(within(source).getByRole("button", { name: "Cerrar" }));
  expect(document.querySelector(".notes-focus")).toBeNull();
  expect(chip).toHaveFocus();
});

it("a chip for a section the document no longer has is disabled", async () => {
  renderPage({
    [`${BASE}/tutor`]: jsonResponse({
      turns: [written("Eso estaba en [§consecuencias].", { sections: [{ anchor: "consecuencias", title: "3. Consecuencias" }] })],
    }),
  });
  await screen.findByRole("heading", { name: /Causas/ });
  const chip = await within(chatRegion()).findByRole("button", { name: "Ir a la sección 3. Consecuencias" });
  expect(chip).toBeDisabled();
  expect(chip).toHaveAttribute("title", "Esa sección ya no está en los apuntes");
});

it("a question asked on the study screen streams its answer with chips into the document", async () => {
  const fetchMock = renderPage({
    [`POST ${BASE}/tutor`]: () =>
      sseResponse([
        ["reply.delta", { text: "Empezó en Gran Bretaña ", attempt: 1 }],
        ["result", { ...written("Empezó en Gran Bretaña [§contexto].[^p1]"), sections: [{ anchor: "contexto", title: "1. Contexto" }] }],
      ]),
  }).fetchMock;
  await screen.findByRole("heading", { name: /Causas/ });
  const input = within(chatRegion()).getByRole("textbox", { name: "Tu pregunta" });
  await waitFor(() => expect(input).toBeEnabled());

  fireEvent.change(input, { target: { value: "¿Dónde empezó?" } });
  fireEvent.click(within(chatRegion()).getByRole("button", { name: "Preguntar" }));

  fireEvent.click(await within(chatRegion()).findByRole("button", { name: "Ir a la sección 1. Contexto" }));
  await waitFor(() => expect(block(/Contexto/)).toHaveClass("notes-focus"));
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ question: "¿Dónde empezó?", style: "written" });
});

// ---- Materials generated from the chat («hazme un quiz», #366, #367) ----

function generation(option: string, kind: string, study: unknown, reply = "Listo: 10 preguntas. Ábrelo en «Quiz».") {
  return sseResponse([
    ["generation.started", { kind, option, text: "Preparando un quiz de 10 preguntas con tus apuntes v5…" }],
    ["result", { kind: "generation", option, material_kind: kind, reply, items: 10, warnings: [], study }],
  ]);
}

async function askInChat(text: string) {
  const input = within(chatRegion()).getByRole("textbox", { name: "Tu pregunta" });
  await waitFor(() => expect(input).toBeEnabled());
  fireEvent.change(input, { target: { value: text } });
  fireEvent.click(within(chatRegion()).getByRole("button", { name: "Preguntar" }));
}

it("a quiz generated from the chat turns its badge to Listo and «Abrir «Quiz»» opens it beside the document", async () => {
  const { fetchMock } = renderPage({
    [`${BASE}/study`]: jsonResponse(studyBody({ quiz: ["sin_generar", null] })),
    [`POST ${BASE}/tutor`]: () => generation("quiz", "quiz", studyBody()),
  });
  await options();
  expect(within(optionButton(/^Quiz/)).getByText("Sin generar")).toBeInTheDocument();

  await askInChat("hazme un quiz");

  await waitFor(() => expect(within(optionButton(/^Quiz/)).getByText("Listo")).toBeInTheDocument());
  // The state came with the result: no second read.
  expect(fetchMock.mock.calls.filter(([url]) => url === `${BASE}/study`)).toHaveLength(1);
  expect(screen.queryByRole("region", { name: "Quiz" })).toBeNull();

  fireEvent.click(within(chatRegion()).getByRole("button", { name: "Abrir «Quiz»" }));

  const panel = screen.getByRole("region", { name: "Quiz" });
  expect(await within(panel).findByText("¿Dónde empezó la Revolución Industrial?")).toBeInTheDocument();
  expect(optionButton(/^Quiz/)).toHaveAttribute("aria-expanded", "true");
});

it("a stale option regenerated from the chat drops its Desactualizado note and reads the material again", async () => {
  const { fetchMock } = renderPage({
    [`${BASE}/study`]: jsonResponse(MIXED),
    [`POST ${BASE}/tutor`]: () =>
      generation("quiz", "quiz", studyBody({ ejercicios: ["sin_generar", null], examen: ["sin_generar", null] })),
  });
  await options();
  fireEvent.click(optionButton(/^Quiz/));
  const panel = screen.getByRole("region", { name: "Quiz" });
  await within(panel).findByText("¿Dónde empezó la Revolución Industrial?");
  expect(within(panel).getByRole("note")).toHaveTextContent("Desactualizado");
  const quizReads = () => fetchMock.mock.calls.filter(([url]) => url === `${BASE}/quiz`).length;
  const before = quizReads();

  await askInChat("hazme un quiz de nuevo");

  await waitFor(() => expect(within(optionButton(/^Quiz/)).getByText("Listo")).toBeInTheDocument());
  await waitFor(() => expect(within(screen.getByRole("region", { name: "Quiz" })).queryByRole("note")).toBeNull());
  await waitFor(() => expect(quizReads()).toBeGreaterThan(before));
  expect(within(optionButton(/^Ejercicios/)).getByText("Sin generar")).toBeInTheDocument();

  // «Abrir» on the option already open reads it again too, and keeps it open.
  const afterResult = quizReads();
  fireEvent.click(within(chatRegion()).getByRole("button", { name: "Abrir «Quiz»" }));
  await waitFor(() => expect(quizReads()).toBeGreaterThan(afterResult));
  expect(screen.getByRole("region", { name: "Quiz" })).toBeInTheDocument();
});

it("the phrase of an option's hint fills the chat's input without sending it", async () => {
  const { fetchMock } = renderPage({ [`${BASE}/study`]: jsonResponse(MIXED) });
  await options();
  fireEvent.click(optionButton(/^Ejercicios/));
  const panel = screen.getByRole("region", { name: "Ejercicios" });
  const input = within(chatRegion()).getByRole("textbox", { name: "Tu pregunta" });
  await waitFor(() => expect(input).toBeEnabled());

  fireEvent.click(within(panel).getByRole("button", { name: "hazme ejercicios" }));

  await waitFor(() => expect(input).toHaveValue("hazme ejercicios"));
  expect(input).toHaveFocus();
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});
