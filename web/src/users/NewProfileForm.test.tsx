import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import { ConfirmProvider } from "../ui/ConfirmDialog";
import UserGate from "./UserGate";

const LUIS = { id: "luis", name: "Luis" };
const calls = (fetchMock: ReturnType<typeof stubApi>, method: string) =>
  fetchMock.mock.calls.filter(([, init]) => init?.method === method);

function clearCookie() {
  document.cookie = "sa_user=; Path=/; Max-Age=0";
}

async function openForm(routes: Parameters<typeof stubApi>[0], users: unknown[] = [LUIS]) {
  const fetchMock = stubApi({ "/api/users": jsonResponse({ users }), ...routes });
  render(
    <ConfirmProvider>
      <UserGate pathname="/">
        <p>contenido</p>
      </UserGate>
    </ConfirmProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Nuevo perfil" }));
  return fetchMock;
}

beforeEach(clearCookie);
afterEach(() => {
  cleanup();
  clearCookie();
  vi.unstubAllGlobals();
});

describe("NewProfileForm", () => {
  it("creates a profile with name and email and chooses it", async () => {
    const fetchMock = await openForm({ "POST /api/users": jsonResponse({ id: "eva", name: "Eva", email: "eva@example.com" }, 201) });
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: " Eva " } });
    fireEvent.change(screen.getByLabelText(/^Correo electrónico/), { target: { value: "eva@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Crear perfil" }));
    expect(await screen.findByText("contenido")).toBeTruthy();
    expect(document.cookie).toContain("sa_user=eva");
    expect(JSON.parse(String(calls(fetchMock, "POST")[0][1]?.body))).toEqual({ name: "Eva", email: "eva@example.com" });
  });

  it("sends no email when it is empty, and uploads the chosen photo to the new id", async () => {
    const fetchMock = await openForm({
      "POST /api/users": jsonResponse({ id: "eva", name: "Eva" }, 201),
      "PUT /api/users/eva/photo": jsonResponse({ id: "eva", name: "Eva", photo_url: "/api/users/eva/photo?v=1" }),
    });
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: "Eva" } });
    const file = new File(["x"], "a.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Elegir foto"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "Crear perfil" }));
    expect(await screen.findByText("contenido")).toBeTruthy();
    expect(JSON.parse(String(calls(fetchMock, "POST")[0][1]?.body))).toEqual({ name: "Eva" });
    expect(calls(fetchMock, "PUT")).toHaveLength(1);
  });

  it("refuses an empty name, a bad email and a bad photo without sending anything", async () => {
    const fetchMock = await openForm({});
    fireEvent.click(screen.getByRole("button", { name: "Crear perfil" }));
    expect(await screen.findByText("El nombre es obligatorio")).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: "Eva" } });
    fireEvent.change(screen.getByLabelText(/^Correo electrónico/), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Crear perfil" }));
    expect(await screen.findByText("El correo electrónico no es válido")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Elegir foto"), { target: { files: [new File(["x"], "a.gif", { type: "image/gif" })] } });
    expect(await screen.findByText(/Formato de foto no admitido/)).toBeTruthy();
    expect(calls(fetchMock, "POST")).toHaveLength(0);
  });

  it("shows the backend's refusal and stays on the form", async () => {
    await openForm({ "POST /api/users": jsonResponse({ detail: "Ya existe un perfil así" }, 409) });
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: "Eva" } });
    fireEvent.click(screen.getByRole("button", { name: "Crear perfil" }));
    expect(await screen.findByText("Ya existe un perfil así")).toBeTruthy();
    expect(screen.queryByText("contenido")).toBeNull();
  });

  it("cancels back to the list, and is offered with zero users", async () => {
    await openForm({}, []);
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.getByText(/No hay usuarios/)).toBeTruthy());
  });
});
