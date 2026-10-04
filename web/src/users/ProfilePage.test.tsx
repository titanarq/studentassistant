import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import { ConfirmProvider } from "../ui/ConfirmDialog";
import ProfilePage from "./ProfilePage";
import UserGate from "./UserGate";

const ANA = { id: "ana", name: "Ana López", email: "ana@example.com" };

function clearCookie() {
  document.cookie = "sa_user=; Path=/; Max-Age=0";
}

async function renderProfile(extra: Parameters<typeof stubApi>[0] = {}) {
  document.cookie = "sa_user=ana; Path=/";
  const fetchMock = stubApi({ "/api/users": jsonResponse({ users: [ANA] }), ...extra });
  render(
    <UserGate pathname="/profile">
      <ProfilePage />
    </UserGate>,
    { wrapper: ConfirmProvider },
  );
  await screen.findByRole("heading", { name: "Editar perfil" });
  return fetchMock;
}

const patches = (fetchMock: ReturnType<typeof stubApi>) =>
  fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");

beforeEach(clearCookie);
afterEach(() => {
  cleanup();
  clearCookie();
  vi.unstubAllGlobals();
});

describe("ProfilePage", () => {
  it("shows the current name and email and no id field", async () => {
    await renderProfile();
    expect((screen.getByLabelText(/^Nombre/) as HTMLInputElement).value).toBe("Ana López");
    expect((screen.getByLabelText(/^Correo electrónico/) as HTMLInputElement).value).toBe("ana@example.com");
    expect(screen.queryByLabelText(/id/i)).toBeNull();
  });

  it("refuses an empty name, a long name and a bad email without sending anything", async () => {
    const fetchMock = await renderProfile();
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: "  " } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByText("El nombre es obligatorio")).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: "x".repeat(81) } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByText("El nombre no puede pasar de 80 caracteres")).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: "Ana" } });
    fireEvent.change(screen.getByLabelText(/^Correo electrónico/), { target: { value: "no-es-correo" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByText("El correo electrónico no es válido")).toBeTruthy();
    expect(patches(fetchMock)).toHaveLength(0);
  });

  it("sends only the changed fields and confirms", async () => {
    const fetchMock = await renderProfile({
      "PATCH /api/users/ana": jsonResponse({ id: "ana", name: "Ana María", email: "ana@example.com" }),
    });
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: " Ana María " } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByText("Perfil guardado")).toBeTruthy();
    expect(JSON.parse(patches(fetchMock)[0][1]!.body as string)).toEqual({ name: "Ana María" });
  });

  it("clears the email with an empty string", async () => {
    const fetchMock = await renderProfile({ "PATCH /api/users/ana": jsonResponse({ id: "ana", name: "Ana López" }) });
    fireEvent.change(screen.getByLabelText(/^Correo electrónico/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    await screen.findByText("Perfil guardado");
    expect(JSON.parse(patches(fetchMock)[0][1]!.body as string)).toEqual({ email: "" });
  });

  it("shows the backend's detail on a 422", async () => {
    await renderProfile({ "PATCH /api/users/ana": jsonResponse({ detail: "Nombre no válido" }, 422) });
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: "Otra" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByText("Nombre no válido")).toBeTruthy();
    expect(screen.queryByText("Perfil guardado")).toBeNull();
  });

  it("goes to / on Cancelar when there is no history", async () => {
    await renderProfile();
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    vi.spyOn(window.history, "length", "get").mockReturnValue(1);
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
  });
});
