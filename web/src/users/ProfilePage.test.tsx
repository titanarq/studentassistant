import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  it("«Cambiar de perfil» forgets the user and shows the selection", async () => {
    await renderProfile();
    fireEvent.click(screen.getByRole("button", { name: "Cambiar de perfil" }));
    expect(await screen.findByRole("heading", { name: "¿Quién eres?" })).toBeTruthy();
    expect(document.cookie).not.toContain("sa_user=ana");
  });

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

  const calls = (fetchMock: ReturnType<typeof stubApi>, method: string) =>
    fetchMock.mock.calls.filter(([, init]) => init?.method === method);

  it("refuses a wrong type and an oversized photo without a request", async () => {
    const fetchMock = await renderProfile();
    const input = screen.getByLabelText("Elegir foto");
    fireEvent.change(input, { target: { files: [new File(["x"], "a.gif", { type: "image/gif" })] } });
    expect(await screen.findByText("Formato de foto no admitido. Usa JPG, PNG o WebP.")).toBeTruthy();
    const big = new File(["x"], "a.png", { type: "image/png" });
    Object.defineProperty(big, "size", { value: 5 * 1024 * 1024 + 1 });
    fireEvent.change(input, { target: { files: [big] } });
    expect(await screen.findByText("La foto es demasiado grande (máximo 5 MiB).")).toBeTruthy();
    expect(calls(fetchMock, "PUT")).toHaveLength(0);
  });

  it("uploads the raw file and shows the new photo everywhere", async () => {
    URL.createObjectURL = vi.fn(() => "blob:preview");
    URL.revokeObjectURL = vi.fn();
    const fetchMock = await renderProfile({
      "PUT /api/users/ana/photo": jsonResponse({ ...ANA, photo_url: "/api/users/ana/photo?v=2" }),
    });
    const file = new File(["img"], "a.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("Elegir foto"), { target: { files: [file] } });
    await waitFor(() => expect(document.querySelectorAll('img[src="/api/users/ana/photo?v=2"]').length).toBeGreaterThan(1));
    const put = calls(fetchMock, "PUT")[0];
    expect(put[1]!.body).toBe(file);
    expect((put[1]!.headers as Record<string, string>)["Content-Type"]).toBe("image/png");
    expect(screen.getByRole("button", { name: "Quitar foto" })).toBeTruthy();
  });

  it("shows the Spanish message of a 413", async () => {
    URL.createObjectURL = vi.fn(() => "blob:preview");
    URL.revokeObjectURL = vi.fn();
    await renderProfile({ "PUT /api/users/ana/photo": jsonResponse({}, 413) });
    fireEvent.change(screen.getByLabelText("Elegir foto"), {
      target: { files: [new File(["i"], "a.png", { type: "image/png" })] },
    });
    expect(await screen.findByText("La foto es demasiado grande (máximo 5 MiB).")).toBeTruthy();
  });

  it("offers «Quitar foto» only with a photo, asks, then deletes", async () => {
    document.cookie = "sa_user=ana; Path=/";
    const withPhoto = { ...ANA, photo_url: "/api/users/ana/photo?v=1" };
    const fetchMock = stubApi({
      "/api/users": jsonResponse({ users: [withPhoto] }),
      "DELETE /api/users/ana/photo": jsonResponse(ANA),
    });
    render(
      <UserGate pathname="/profile">
        <ProfilePage />
      </UserGate>,
      { wrapper: ConfirmProvider },
    );
    fireEvent.click(await screen.findByRole("button", { name: "Quitar foto" }));
    expect(calls(fetchMock, "DELETE")).toHaveLength(0);
    const dialog = await screen.findByRole("dialog", { name: "¿Quitar la foto?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Quitar foto" }));
    await waitFor(() => expect(calls(fetchMock, "DELETE")).toHaveLength(1));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Quitar foto" })).toBeNull());
  });

  it("has no «Quitar foto» without a photo", async () => {
    await renderProfile();
    expect(screen.queryByRole("button", { name: "Quitar foto" })).toBeNull();
  });

  it("updates the app bar avatar and name after saving", async () => {
    await renderProfile({ "PATCH /api/users/ana": jsonResponse({ id: "ana", name: "Ana María", email: "ana@example.com" }) });
    fireEvent.change(screen.getByLabelText(/^Nombre/), { target: { value: "Ana María" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    await screen.findByText("Perfil guardado");
    expect(screen.getByLabelText("Menú de Ana María")).toBeTruthy();
  });
});
