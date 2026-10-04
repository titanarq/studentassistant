import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import { ConfirmProvider } from "../ui/ConfirmDialog";
import UserGate from "./UserGate";

const USERS = { users: [{ id: "ana", name: "Ana López" }] };

function clearCookie() {
  document.cookie = "sa_user=; Path=/; Max-Age=0";
}

async function signedIn() {
  stubApi({ "/api/users": jsonResponse(USERS) });
  render(
    <ConfirmProvider>
      <UserGate pathname="/">
        <p>contenido</p>
      </UserGate>
    </ConfirmProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /Ana López/ }));
  return screen.getByRole("button", { name: "Menú de Ana López" });
}

beforeEach(clearCookie);
afterEach(() => {
  cleanup();
  clearCookie();
  vi.unstubAllGlobals();
});

describe("AppBar", () => {
  it("shows the avatar button with initials and publishes --app-bar-height while mounted", async () => {
    const button = await signedIn();
    expect(button.textContent).toBe("AL");
    expect(document.documentElement.style.getPropertyValue("--app-bar-height")).toMatch(/px$/);
    cleanup();
    expect(document.documentElement.style.getPropertyValue("--app-bar-height")).toBe("");
  });

  it("is not shown on /pair", () => {
    render(
      <ConfirmProvider>
        <UserGate pathname="/pair">
          <p>emparejar</p>
        </UserGate>
      </ConfirmProvider>,
    );
    expect(screen.queryByRole("button", { name: /Menú de/ })).toBeNull();
  });

  it("opens the menu with the keyboard, moves with arrows and closes with Escape returning focus", async () => {
    const button = await signedIn();
    fireEvent.keyDown(button, { key: "ArrowDown" });
    const edit = await screen.findByRole("menuitem", { name: "Editar perfil" });
    const out = screen.getByRole("menuitem", { name: "Cerrar sesión" });
    await waitFor(() => expect(document.activeElement).toBe(edit));
    fireEvent.keyDown(edit, { key: "ArrowDown" });
    expect(document.activeElement).toBe(out);
    fireEvent.keyDown(out, { key: "ArrowDown" });
    expect(document.activeElement).toBe(edit);
    fireEvent.keyDown(edit, { key: "ArrowUp" });
    expect(document.activeElement).toBe(out);
    fireEvent.keyDown(out, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("asks before signing out; cancelling keeps the user, confirming deletes the cookie and shows the selection", async () => {
    const button = await signedIn();
    expect(document.cookie).toContain("sa_user=ana");
    fireEvent.click(button);
    fireEvent.click(screen.getByRole("menuitem", { name: "Cerrar sesión" }));
    expect(await screen.findByText("¿Cerrar la sesión de Ana López?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.queryByText("¿Cerrar la sesión de Ana López?")).toBeNull());
    expect(screen.getByText("contenido")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Menú de Ana López" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Cerrar sesión" }));
    await screen.findByText("¿Cerrar la sesión de Ana López?");
    fireEvent.click(screen.getAllByRole("button", { name: "Cerrar sesión" })[0]);
    expect(await screen.findByRole("heading", { name: "¿Quién eres?" })).toBeTruthy();
    expect(document.cookie).not.toContain("sa_user=ana");
  });
});
