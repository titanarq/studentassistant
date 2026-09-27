import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import ModeSwitch from "./ModeSwitch";
import { PAGE_TEST_TIMEOUT } from "../test/timeouts";

const API = "/api/subjects/historia/topics/revolucion-industrial/study";
const PAGE = "/subjects/historia/topics/revolucion-industrial";

const STATE = {
  subject: "historia",
  topic: "revolucion-industrial",
  study_version: { version: 5, tag: "historia/revolucion-industrial/apuntes-v5", marked_at: "2026-09-26T18:58:00Z" },
  study_current: true,
  options: [{ key: "quiz", kind: "quiz", state: "listo", stale_reason: null, notes_version: 5 }],
  created_tag: true,
  ended_session: "s-20260926-1000",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderSwitch(current: "build" | "study" = "build") {
  const navigate = vi.fn();
  render(<ModeSwitch subjectId="historia" topicId="revolucion-industrial" current={current} navigate={navigate} />);
  const modes = screen.getByRole("navigation", { name: "Modo del tema" });
  return { navigate, modes };
}

function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

it("switches to Estudiar through the backend, then opens the study screen", async () => {
  const answer = deferred();
  const fetchMock = stubApi({ [`POST ${API}`]: () => answer.promise });
  const { navigate, modes } = renderSwitch();
  const study = within(modes).getByRole("link", { name: "Estudiar" });
  expect(within(modes).getByRole("link", { name: "Construir" })).toHaveAttribute("aria-current", "page");

  fireEvent.click(study);

  expect(within(modes).getByRole("link", { name: "Pasando a Estudiar…" })).toHaveAttribute("aria-disabled", "true");
  // A second click while it runs sends nothing more.
  fireEvent.click(within(modes).getByRole("link", { name: "Pasando a Estudiar…" }));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock).toHaveBeenCalledWith(API, { method: "POST" });
  expect(navigate).not.toHaveBeenCalled();

  await act(async () => answer.resolve(jsonResponse(STATE)));

  expect(navigate).toHaveBeenCalledWith(`${PAGE}/study`);
  expect(screen.queryByRole("alert")).toBeNull();
}, PAGE_TEST_TIMEOUT);

it("asks to wait while the notes are being prepared, and stays", async () => {
  stubApi({
    [`POST ${API}`]: jsonResponse({ detail: "Los apuntes están ocupados.", code: "notes_busy" }, 409),
  });
  const { navigate, modes } = renderSwitch();

  fireEvent.click(within(modes).getByRole("link", { name: "Estudiar" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Se están preparando los apuntes; espera a que terminen.");
  expect(within(modes).getByRole("link", { name: "Estudiar" })).not.toHaveAttribute("aria-disabled");
  expect(navigate).not.toHaveBeenCalled();
});

it("gives the backend's reason when the topic has no notes, and stays", async () => {
  stubApi({
    [`POST ${API}`]: jsonResponse({ detail: "El tema todavía no tiene apuntes." }, 409),
  });
  const { navigate, modes } = renderSwitch();

  fireEvent.click(within(modes).getByRole("link", { name: "Estudiar" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("El tema todavía no tiene apuntes.");
  expect(navigate).not.toHaveBeenCalled();
});

it("says so when the backend cannot be reached", async () => {
  stubApi({ [`POST ${API}`]: new TypeError("network") });
  const { navigate, modes } = renderSwitch();

  fireEvent.click(within(modes).getByRole("link", { name: "Estudiar" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo conectar con el servidor.");
  expect(navigate).not.toHaveBeenCalled();
});

it("keeps Construir a plain link and posts nothing from the study screen", () => {
  const fetchMock = stubApi({});
  const { navigate, modes } = renderSwitch("study");
  const build = within(modes).getByRole("link", { name: "Construir" });
  const study = within(modes).getByRole("link", { name: "Estudiar" });
  expect(build).toHaveAttribute("href", `${PAGE}/workspace`);
  expect(study).toHaveAttribute("aria-current", "page");

  // jsdom cannot follow a link: the browser's own navigation is what is left out here.
  const noNavigation = (event: Event) => event.preventDefault();
  window.addEventListener("click", noNavigation);
  try {
    fireEvent.click(build);
    fireEvent.click(study);
  } finally {
    window.removeEventListener("click", noNavigation);
  }

  expect(fetchMock).not.toHaveBeenCalled();
  expect(navigate).not.toHaveBeenCalled();
});
