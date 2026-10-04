import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import UserGate from "./UserGate";
import { reportUserError } from "./userRequired";

const USERS = {
  users: [
    { id: "ana", name: "Ana López", email: "ana@example.com", photo_url: "/api/users/ana/photo" },
    { id: "luis", name: "Luis" },
  ],
};

function clearCookie() {
  document.cookie = "sa_user=; Path=/; Max-Age=0";
}

function renderGate(pathname = "/") {
  return render(
    <UserGate pathname={pathname}>
      <p>contenido</p>
    </UserGate>,
  );
}

beforeEach(clearCookie);
afterEach(() => {
  cleanup();
  clearCookie();
  vi.unstubAllGlobals();
});

describe("UserGate", () => {
  it("shows one card per user, with photo or initials, and renders the page once one is chosen", async () => {
    stubApi({ "/api/users": jsonResponse(USERS) });
    renderGate();
    expect(await screen.findByRole("heading", { name: "¿Quién eres?" })).toBeTruthy();
    expect(screen.queryByText("contenido")).toBeNull();
    expect(screen.getByText("ana@example.com")).toBeTruthy();
    expect(screen.getByText("LU")).toBeTruthy();
    expect(document.querySelector('img[src="/api/users/ana/photo"]')).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Luis/ }));
    expect(screen.getByText("contenido")).toBeTruthy();
    expect(document.cookie).toContain("sa_user=luis");
  });

  it("writes nothing to web storage and asks again after a reload even with the cookie set", async () => {
    stubApi({ "/api/users": jsonResponse(USERS) });
    const local = vi.spyOn(Storage.prototype, "setItem");
    const first = renderGate();
    fireEvent.click(await screen.findByRole("button", { name: /Ana/ }));
    expect(local).not.toHaveBeenCalled();
    first.unmount();
    renderGate();
    expect(await screen.findByRole("heading", { name: "¿Quién eres?" })).toBeTruthy();
    expect(screen.queryByText("contenido")).toBeNull();
    local.mockRestore();
  });

  it("explains an empty vault and a failed list, and retries", async () => {
    stubApi({ "/api/users": jsonResponse({ users: [] }) });
    renderGate();
    expect(await screen.findByText(/No hay usuarios/)).toBeTruthy();
    cleanup();
    let calls = 0;
    stubApi({ "/api/users": () => (++calls === 1 ? jsonResponse({ detail: "x" }, 500) : jsonResponse(USERS)) });
    renderGate();
    fireEvent.click(await screen.findByRole("button", { name: "Reintentar" }));
    expect(await screen.findByRole("button", { name: /Ana/ })).toBeTruthy();
  });

  it("shows the selection with a note when another tab switched the user", async () => {
    stubApi({ "/api/users": jsonResponse(USERS) });
    renderGate();
    fireEvent.click(await screen.findByRole("button", { name: /Ana/ }));
    document.cookie = "sa_user=luis; Path=/";
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(await screen.findByText("Se ha cambiado de usuario en otra pestaña")).toBeTruthy();
    expect(screen.queryByText("contenido")).toBeNull();
  });

  it("does not react to focus while the cookie still names the user", async () => {
    stubApi({ "/api/users": jsonResponse(USERS) });
    renderGate();
    fireEvent.click(await screen.findByRole("button", { name: /Ana/ }));
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(screen.getByText("contenido")).toBeTruthy();
  });

  it("shows the selection again on a user_required or user_not_found answer", async () => {
    stubApi({ "/api/users": jsonResponse(USERS) });
    renderGate();
    fireEvent.click(await screen.findByRole("button", { name: /Ana/ }));
    expect(reportUserError({ detail: "x" })).toBe(false);
    expect(screen.getByText("contenido")).toBeTruthy();
    act(() => {
      expect(reportUserError({ detail: "Falta el usuario.", code: "user_required" })).toBe(true);
    });
    expect(await screen.findByRole("heading", { name: "¿Quién eres?" })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /Ana/ }));
    act(() => {
      reportUserError({ detail: "No existe.", code: "user_not_found" });
    });
    expect(await screen.findByRole("heading", { name: "¿Quién eres?" })).toBeTruthy();
  });

  it("does not gate /pair", () => {
    const fetchMock = stubApi({});
    renderGate("/pair");
    expect(screen.getByText("contenido")).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
