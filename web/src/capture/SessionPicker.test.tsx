import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Session } from "../protocol";
import SessionPicker, { type OpenedSession } from "./SessionPicker";

// Contract-valid bodies, shaped exactly like `protocol/examples/rest.*` of the same message: the
// picker decodes every one of them through the bindings, so a mistake here fails the test loudly.

const NOW = 1790251200000;

const SUBJECTS = [
  { subject_id: "biologia", name: "Biología" },
  { subject_id: "historia", name: "Historia de España" },
];

const BIOLOGY_TOPICS = [
  {
    topic_id: "la-celula",
    subject_id: "biologia",
    name: "La célula",
    open_session_id: "s-20260924-1805",
    last_session_at_ms: 1790273100000,
    pending_count: 2,
  },
  { topic_id: "fotosintesis", subject_id: "biologia", name: "Fotosíntesis" },
];

const STARTED: Session = {
  session_id: "s-20260924-1810",
  subject_id: "biologia",
  topic_id: "fotosintesis",
  status: "active",
  started_at_ms: 1790251200120,
  ws_path: "/ws/sessions/s-20260924-1810",
  protocol_version: "1.1",
  received_capture_ids: [],
};

const RESUMED: Session = {
  ...STARTED,
  session_id: "s-20260924-1805",
  topic_id: "la-celula",
  started_at_ms: 1790273100000,
  ws_path: "/ws/sessions/s-20260924-1805",
};

interface Sent {
  readonly path: string;
  readonly init: RequestInit;
}

const sent: Sent[] = [];

type Route = (init: RequestInit) => Response | Promise<Response>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(routes: Record<string, Route>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit = {}) => {
      sent.push({ path, init });
      const route = routes[path];
      return route === undefined
        ? jsonResponse({ detail: `Ninguna ruta de prueba responde a ${path}.` }, 404)
        : await route(init);
    }),
  );
}

/** The backend of a normal run: two subjects, one of them with a topic that has an unended session. */
function backend(routes: Record<string, Route> = {}): void {
  stubFetch({
    "/api/subjects": () => jsonResponse({ subjects: SUBJECTS }),
    "/api/subjects/biologia/topics": () =>
      jsonResponse({ subject_id: "biologia", topics: BIOLOGY_TOPICS }),
    "/api/subjects/historia/topics": () => jsonResponse({ subject_id: "historia", topics: [] }),
    "/api/sessions": () => jsonResponse(STARTED, 201),
    "/api/sessions/s-20260924-1805/resume": () => jsonResponse(RESUMED),
    ...routes,
  });
}

function renderPicker(onSession?: (opened: OpenedSession) => void) {
  render(<SessionPicker now={() => NOW} onSession={onSession} />);
}

async function chooseTopic(subject: string, topic: string): Promise<void> {
  fireEvent.click(await screen.findByRole("button", { name: subject }));
  fireEvent.click(await screen.findByRole("button", { name: topic }));
}

function callsTo(path: string): RequestInit[] {
  return sent.filter((each) => each.path === path).map((each) => each.init);
}

beforeEach(() => {
  sent.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("lists the subjects and then the topics of the chosen one", async () => {
  backend();
  renderPicker();

  expect(screen.getByText("Cargando las asignaturas…")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Historia de España" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Biología" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: /^Temas/ })).not.toBeInTheDocument();
  expect(sent).toEqual([{ path: "/api/subjects", init: { method: "GET" } }]);

  fireEvent.click(screen.getByRole("button", { name: "Historia de España" }));

  expect(screen.getByText("Cargando los temas…")).toBeInTheDocument();
  expect(
    await screen.findByText("Esta asignatura todavía no tiene ningún tema: crea el primero."),
  ).toBeInTheDocument();
  expect(sent[1]).toEqual({ path: "/api/subjects/historia/topics", init: { method: "GET" } });
  expect(screen.getByRole("form", { name: "Crear un tema" })).toBeInTheDocument();
});

it("marks the unended session and the pending doubts of a topic, and the chosen item of each list", async () => {
  backend();
  renderPicker();
  fireEvent.click(await screen.findByRole("button", { name: "Biología" }));

  const unended = await screen.findByRole("button", { name: "La célula" });
  expect(unended).not.toHaveAttribute("aria-current");
  expect(unended.closest("li")).toHaveTextContent("(sesión sin terminar, 2 dudas pendientes)");
  expect(screen.getByRole("button", { name: "Fotosíntesis" }).closest("li")).not.toHaveTextContent(
    "sesión sin terminar",
  );

  fireEvent.click(unended);

  expect(unended).toHaveAttribute("aria-current", "true");
  expect(screen.getByRole("heading", { name: "Temas de Biología" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "La sesión" })).toBeInTheDocument();
});

it("offers to continue a topic with an unended session and to start one for a topic without it", async () => {
  backend();
  renderPicker();
  await chooseTopic("Biología", "La célula");

  expect(
    screen.getByText("Este tema tiene una sesión sin terminar: puedes continuarla donde la dejaste."),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Continuar la sesión abierta" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Empezar una sesión nueva" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Fotosíntesis" }));

  expect(screen.getByText("Este tema no tiene ninguna sesión sin terminar.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Empezar una sesión nueva" })).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Continuar la sesión abierta" }),
  ).not.toBeInTheDocument();
});

it("starts a new session on a topic with no unended one and hands it to the capture screen", async () => {
  backend();
  const onSession = vi.fn();
  renderPicker(onSession);
  await chooseTopic("Biología", "Fotosíntesis");

  fireEvent.click(screen.getByRole("button", { name: "Empezar una sesión nueva" }));

  expect(await screen.findByRole("status")).toHaveTextContent(
    "Sesión s-20260924-1810 en marcha: Biología, Fotosíntesis.",
  );
  expect(sent[2]).toEqual({
    path: "/api/sessions",
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subject_id: "biologia",
        topic_id: "fotosintesis",
        client_time_ms: NOW,
      }),
    },
  });
  expect(onSession).toHaveBeenCalledTimes(1);
  expect(onSession.mock.calls[0][0]).toEqual({
    session: STARTED,
    subjectName: "Biología",
    topicName: "Fotosíntesis",
  });
  // The lists give way to the open session: the topic cannot change inside it.
  expect(screen.queryByRole("button", { name: "Biología" })).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Empezar una sesión nueva" }),
  ).not.toBeInTheDocument();
});

it("continues the topic's unended session instead of starting one", async () => {
  backend();
  const onSession = vi.fn();
  renderPicker(onSession);
  await chooseTopic("Biología", "La célula");

  fireEvent.click(screen.getByRole("button", { name: "Continuar la sesión abierta" }));

  expect(await screen.findByRole("status")).toHaveTextContent(
    "Sesión s-20260924-1805 en marcha: Biología, La célula.",
  );
  expect(sent[2]).toEqual({
    path: "/api/sessions/s-20260924-1805/resume",
    init: { method: "POST" },
  });
  expect(sent.some((each) => each.path === "/api/sessions")).toBe(false);
  expect(onSession).toHaveBeenCalledWith({
    session: RESUMED,
    subjectName: "Biología",
    topicName: "La célula",
  });
});

it("disables the action and says in Spanish that the session is being opened", async () => {
  let release!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  backend({ "/api/sessions": () => pending });
  renderPicker();
  await chooseTopic("Biología", "Fotosíntesis");

  fireEvent.click(screen.getByRole("button", { name: "Empezar una sesión nueva" }));

  expect(screen.getByRole("button", { name: "Abriendo la sesión…" })).toBeDisabled();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();

  await act(async () => {
    release(jsonResponse(STARTED, 201));
  });

  expect(await screen.findByRole("status")).toHaveTextContent("s-20260924-1810");
});

it("creates a subject and a topic, re-reading each list, and leaves the new topic chosen", async () => {
  const createdSubject = { subject_id: "fisica", name: "Física" };
  const createdTopic = { topic_id: "cinematica", subject_id: "fisica", name: "Cinemática" };
  const subjects = [...SUBJECTS];
  const fisicaTopics: unknown[] = [];
  backend({
    "/api/subjects": (init) => {
      if (init.method === "POST") {
        subjects.push(createdSubject);
        return jsonResponse(createdSubject, 201);
      }
      return jsonResponse({ subjects });
    },
    "/api/subjects/fisica/topics": (init) => {
      if (init.method === "POST") {
        fisicaTopics.push(createdTopic);
        return jsonResponse(createdTopic, 201);
      }
      return jsonResponse({ subject_id: "fisica", topics: fisicaTopics });
    },
  });
  renderPicker();

  fireEvent.change(await screen.findByLabelText("Nombre de la asignatura"), {
    target: { value: " Física " },
  });
  fireEvent.click(screen.getByRole("button", { name: "Crear la asignatura" }));

  expect(await screen.findByRole("button", { name: "Física" })).toHaveAttribute(
    "aria-current",
    "true",
  );
  expect(callsTo("/api/subjects")).toEqual([
    { method: "GET" },
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Física" }),
    },
    { method: "GET" },
  ]);

  fireEvent.change(await screen.findByLabelText("Nombre del tema"), {
    target: { value: "Cinemática" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Crear el tema" }));

  expect(await screen.findByRole("button", { name: "Cinemática" })).toHaveAttribute(
    "aria-current",
    "true",
  );
  expect(callsTo("/api/subjects/fisica/topics")).toEqual([
    { method: "GET" },
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Cinemática" }),
    },
    { method: "GET" },
  ]);
  expect(screen.getByRole("heading", { name: "Temas de Física" })).toBeInTheDocument();

  // The new topic can be opened straight away.
  fireEvent.click(screen.getByRole("button", { name: "Empezar una sesión nueva" }));
  expect(await screen.findByRole("status")).toHaveTextContent(
    "en marcha: Física, Cinemática.",
  );
  expect(JSON.parse(sent[sent.length - 1].init.body as string)).toEqual({
    subject_id: "fisica",
    topic_id: "cinematica",
    client_time_ms: NOW,
  });
});

it("asks for a name in Spanish instead of posting an empty one", async () => {
  backend();
  renderPicker();
  await screen.findByRole("button", { name: "Biología" });

  fireEvent.click(screen.getByRole("button", { name: "Crear la asignatura" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Escribe el nombre de la asignatura.",
  );
  expect(callsTo("/api/subjects")).toEqual([{ method: "GET" }]);
});

it("explains in Spanish a backend that cannot be reached and re-reads the subjects on retry", async () => {
  let attempts = 0;
  stubFetch({
    "/api/subjects": () => {
      attempts += 1;
      return attempts === 1
        ? Promise.reject(new TypeError("Failed to fetch"))
        : jsonResponse({ subjects: SUBJECTS });
    },
  });
  renderPicker();

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se han podido cargar las asignaturas: no se pudo conectar con el servidor.",
  );
  expect(screen.queryByRole("button", { name: "Biología" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));

  expect(await screen.findByRole("button", { name: "Biología" })).toBeInTheDocument();
  expect(attempts).toBe(2);
});

it("explains in Spanish a 2xx body that is not the protocol message, instead of using it", async () => {
  backend({ "/api/subjects": () => jsonResponse({ asignaturas: [] }) });
  renderPicker();

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se han podido cargar las asignaturas: la respuesta del servidor no sigue el protocolo" +
      " esperado (rest.subjects.list.response).",
  );
  expect(screen.queryByRole("button", { name: "Biología" })).not.toBeInTheDocument();
});

it("shows the backend's own Spanish refusal when a subject's topics cannot be read", async () => {
  backend({
    "/api/subjects/biologia/topics": () =>
      jsonResponse({ detail: "No existe esa asignatura." }, 404),
  });
  renderPicker();

  fireEvent.click(await screen.findByRole("button", { name: "Biología" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se han podido cargar los temas: No existe esa asignatura.",
  );
  expect(screen.getByRole("button", { name: "Reintentar" })).toBeInTheDocument();
  expect(screen.getByRole("form", { name: "Crear un tema" })).toBeInTheDocument();
});

it("shows the backend's own Spanish refusal of a start and leaves the picker usable", async () => {
  backend({
    "/api/sessions": () =>
      jsonResponse({ detail: "Ya hay una sesión sin terminar: s-20260924-1805." }, 409),
  });
  const onSession = vi.fn();
  renderPicker(onSession);
  await chooseTopic("Biología", "Fotosíntesis");

  fireEvent.click(screen.getByRole("button", { name: "Empezar una sesión nueva" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se ha podido empezar la sesión: Ya hay una sesión sin terminar: s-20260924-1805.",
  );
  expect(onSession).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Empezar una sesión nueva" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Biología" })).toBeInTheDocument();
});

it("shows the backend's own Spanish refusal of a resume", async () => {
  backend({
    "/api/sessions/s-20260924-1805/resume": () =>
      jsonResponse({ detail: "Esa sesión ya está terminada." }, 409),
  });
  const onSession = vi.fn();
  renderPicker(onSession);
  await chooseTopic("Biología", "La célula");

  fireEvent.click(screen.getByRole("button", { name: "Continuar la sesión abierta" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "No se ha podido continuar la sesión: Esa sesión ya está terminada.",
  );
  expect(onSession).not.toHaveBeenCalled();
});

it("keeps nothing in browser storage and sends no token", async () => {
  const setItem = vi.spyOn(Storage.prototype, "setItem");
  backend();
  renderPicker(vi.fn());
  await chooseTopic("Biología", "Fotosíntesis");

  fireEvent.click(screen.getByRole("button", { name: "Empezar una sesión nueva" }));
  await screen.findByRole("status");

  expect(setItem).not.toHaveBeenCalled();
  expect(sent.map((each) => each.init.headers)).toEqual([
    undefined,
    undefined,
    { "Content-Type": "application/json" },
  ]);
  setItem.mockRestore();
});
