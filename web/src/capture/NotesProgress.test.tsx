/**
 * The notes generation view after "Terminar y preparar apuntes" (#271), against a mocked `fetch`
 * and fake timers: each final status, a transient poll failure, a refusal, and that it stops
 * polling on a final status or when it goes away.
 */

import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import NotesProgress from "./NotesProgress";

const PATH = "/api/subjects/biologia/topics/fotosintesis/notes/generation";
const TOPIC_HREF = "/subjects/biologia/topics/fotosintesis";
const POLL_MS = 500;

let answers: Array<() => Response>;
let polls: number;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function generation(status: string, extra: Record<string, unknown> = {}): () => Response {
  return () =>
    jsonResponse({ subject_id: "biologia", topic_id: "fotosintesis", status, ...extra });
}

type Start = "started" | "running" | "unavailable" | undefined;

function renderView(...args: [] | [Start]) {
  const start: Start = args.length === 0 ? "started" : args[0];
  return render(
    <NotesProgress
      subjectId="biologia"
      topicId="fotosintesis"
      subjectName="Biología"
      topicName="Fotosíntesis"
      start={start}
      intervalMs={POLL_MS}
    />,
  );
}

async function tick(times = 1): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
  }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"], shouldAdvanceTime: true });
  answers = [];
  polls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      if (path !== PATH) return jsonResponse({ detail: "ruta desconocida" }, 404);
      polls += 1;
      const answer = answers.length > 1 ? answers.shift() : answers[0];
      if (answer === undefined) throw new Error("no answer scripted");
      return answer();
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("NotesProgress", () => {
  it("shows the running line until the notes are done, then links to them", async () => {
    answers = [generation("running"), generation("done", { version: 2 })];
    renderView();
    expect(screen.getByRole("status")).toHaveTextContent("Preparando los apuntes…");
    expect(polls).toBe(0);
    await tick();
    expect(polls).toBe(1);
    expect(screen.getByRole("status")).toHaveTextContent("Preparando los apuntes…");
    await tick();
    expect(screen.getByRole("status")).toHaveTextContent("Los apuntes están listos (versión 2).");
    expect(screen.getByRole("link", { name: "Abrir los apuntes" })).toHaveAttribute(
      "href",
      `${TOPIC_HREF}/notes`,
    );
    await tick(4);
    expect(polls).toBe(2);
  });

  it("warns that a draft is not the final version, with the backend's warning", async () => {
    answers = [generation("done", { draft: true, warning: "Falta una página por transcribir." })];
    renderView("running");
    await tick();
    expect(screen.getByRole("status")).toHaveTextContent(
      "El borrador de los apuntes está listo, pero todavía no es la versión definitiva",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Falta una página por transcribir.");
    expect(screen.getByRole("link", { name: "Revisar el borrador" })).toHaveAttribute(
      "href",
      `${TOPIC_HREF}/notes`,
    );
  });

  it("shows a failure's detail and links to the topic to retry", async () => {
    answers = [generation("failed", { detail: "Claude no ha respondido." })];
    renderView();
    await tick();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "No se pudieron preparar los apuntes: Claude no ha respondido.",
    );
    expect(
      screen.getByRole("link", { name: "Ir al tema para volver a intentarlo" }),
    ).toHaveAttribute("href", TOPIC_HREF);
    await tick(3);
    expect(polls).toBe(1);
  });

  it("says the spending cap was reached and sends the student to the topic to confirm", async () => {
    answers = [
      generation("needs_confirmation", { detail: "Se ha alcanzado el límite de gasto diario." }),
    ];
    renderView();
    await tick();
    expect(screen.getByRole("alert")).toHaveTextContent("Se ha alcanzado el límite de gasto diario.");
    expect(screen.getByText(/Prepárame el tema/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ir al tema" })).toHaveAttribute("href", TOPIC_HREF);
    await tick(3);
    expect(polls).toBe(1);
  });

  it("says the backend lost track of a generation it no longer knows", async () => {
    answers = [generation("idle")];
    renderView();
    await tick();
    expect(screen.getByRole("alert")).toHaveTextContent("ya no tiene noticia de la preparación");
  });

  it("keeps polling through a poll that got no answer", async () => {
    answers = [
      () => jsonResponse({}, 503),
      generation("done", { version: 1 }),
    ];
    renderView();
    await tick();
    expect(screen.getByRole("status")).toHaveTextContent("Preparando los apuntes…");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "No se puede consultar el progreso: el servidor respondió con un error (503). Se volverá a intentar.",
    );
    await tick();
    expect(screen.getByRole("status")).toHaveTextContent("Los apuntes están listos (versión 1).");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("stops on a refusal", async () => {
    answers = [() => jsonResponse({ detail: "El tema no existe." }, 404)];
    renderView();
    await tick();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "No se puede consultar la preparación de los apuntes: El tema no existe.",
    );
    await tick(3);
    expect(polls).toBe(1);
  });

  it("treats an end response without notes_generation as unavailable", async () => {
    renderView(undefined);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "El servidor no puede preparar los apuntes ahora",
    );
    await tick(3);
    expect(polls).toBe(0);
  });

  it("stops polling when it goes away", async () => {
    answers = [generation("running")];
    const view = renderView();
    await tick();
    expect(polls).toBe(1);
    view.unmount();
    await tick(4);
    expect(polls).toBe(1);
  });
});
