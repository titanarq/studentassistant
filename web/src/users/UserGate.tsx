import { createContext, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { USER_COOKIE, type User } from "../protocol";
import { listUsers } from "./api";
import AppBar from "./AppBar";
import Avatar from "./Avatar";
import NewProfileForm from "./NewProfileForm";
import { USER_REQUIRED_EVENT } from "./userRequired";
import "./userGate.css";

/**
 * Asks «¿Quién eres?» on every page but `/pair` until a user is chosen and renders its children as
 * that user (epic #544). The choice is the `sa_user` session cookie (no `Max-Age`, so it dies with
 * the browser): a reload or a new window finds it, checks it against the user list and does not ask
 * again; «Cerrar sesión» deletes it.
 */

export interface ActiveUser {
  user: User;
  /** Forgets the user, deletes the cookie and shows the selection. */
  signOut: () => void;
  /** Replaces the in-memory user with the server's updated one (the app bar and the page follow). */
  setUser: (user: User) => void;
}

const ActiveUserContext = createContext<ActiveUser | null>(null);

/** The user the gate let in; only valid under a `UserGate`'s children. */
export function useActiveUser(): ActiveUser {
  const value = useContext(ActiveUserContext);
  if (value === null) throw new Error("useActiveUser needs a UserGate");
  return value;
}

function readCookie(): string | null {
  for (const part of document.cookie.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === USER_COOKIE) return decodeURIComponent(rest.join("="));
  }
  return null;
}

function setCookie(id: string): void {
  document.cookie = `${USER_COOKIE}=${encodeURIComponent(id)}; Path=/; SameSite=Strict`;
}

function deleteCookie(): void {
  document.cookie = `${USER_COOKIE}=; Path=/; SameSite=Strict; Max-Age=0`;
}

type Listing = { state: "loading" } | { state: "error" } | { state: "ready"; users: User[] };

function Selection({ note }: { note: string | null }) {
  const [listing, setListing] = useState<Listing>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [creating, setCreating] = useState(false);
  const choose = useContext(ChooseContext);

  useEffect(() => {
    let cancelled = false;
    setListing({ state: "loading" });
    void listUsers().then((result) => {
      if (!cancelled) setListing(result.kind === "ok" ? { state: "ready", users: result.users } : { state: "error" });
    });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  return (
    <main className="user-gate">
      <h1 className="user-gate-title">{creating ? "Nuevo perfil" : "¿Quién eres?"}</h1>
      {note !== null && (
        <p className="user-gate-note" role="status">
          {note}
        </p>
      )}
      {creating && <NewProfileForm onCreated={choose} onCancel={() => setCreating(false)} />}
      {!creating && listing.state === "loading" && <p className="user-gate-status">Cargando usuarios…</p>}
      {!creating && listing.state === "error" && (
        <div className="user-gate-status" role="alert">
          <p>No se pudo cargar la lista de usuarios.</p>
          <button type="button" className="user-gate-retry" onClick={() => setAttempt((n) => n + 1)}>
            Reintentar
          </button>
        </div>
      )}
      {!creating && listing.state === "ready" && listing.users.length === 0 && (
        <p className="user-gate-status">
          No hay usuarios. Crea el primero con «Nuevo perfil».
        </p>
      )}
      {!creating && listing.state === "ready" && listing.users.length > 0 && (
        <ul className="user-gate-list">
          {listing.users.map((user) => (
            <li key={user.id}>
              <button type="button" className="user-gate-card" onClick={() => choose(user)}>
                <Avatar user={user} />
                <span className="user-gate-name">{user.name}</span>
                {user.email && <small className="user-gate-email">{user.email}</small>}
              </button>
            </li>
          ))}
        </ul>
      )}
      {!creating && listing.state !== "loading" && (
        <button type="button" className="user-gate-new" onClick={() => setCreating(true)}>
          Nuevo perfil
        </button>
      )}
    </main>
  );
}

const ChooseContext = createContext<(user: User, photoFailed?: boolean) => void>(() => {});

const PHOTO_NOTICE = "No se pudo guardar la foto. Puedes añadirla de nuevo desde «Editar perfil».";

export default function UserGate({ children, pathname = window.location.pathname }: { children: ReactNode; pathname?: string }) {
  const [user, setUser] = useState<User | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [photoNotice, setPhotoNotice] = useState(false);
  const [restoring, setRestoring] = useState(() => readCookie() !== null);
  const userRef = useRef<User | null>(null);
  userRef.current = user;

  const choose = useCallback((chosen: User, photoFailed = false) => {
    setCookie(chosen.id);
    setNote(null);
    setPhotoNotice(photoFailed);
    setUser(chosen);
  }, []);

  const signOut = useCallback(() => {
    deleteCookie();
    setNote(null);
    setPhotoNotice(false);
    setUser(null);
  }, []);

  useEffect(() => {
    const id = readCookie();
    if (id === null) return;
    let cancelled = false;
    void listUsers().then((result) => {
      if (cancelled) return;
      const known = result.kind === "ok" ? result.users.find((candidate) => candidate.id === id) : undefined;
      if (known !== undefined) setUser(known);
      setRestoring(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!photoNotice) return;
    const dismiss = () => setPhotoNotice(false);
    window.addEventListener("popstate", dismiss);
    return () => window.removeEventListener("popstate", dismiss);
  }, [photoNotice]);

  useEffect(() => {
    if (userRef.current === null) return;
    const recheck = () => {
      const current = userRef.current;
      if (current === null || document.visibilityState === "hidden") return;
      if (readCookie() !== current.id) {
        setNote("Se ha cambiado de usuario en otra pestaña");
        setUser(null);
      }
    };
    const invalid = () => {
      setNote(null);
      setUser(null);
    };
    document.addEventListener("visibilitychange", recheck);
    window.addEventListener("focus", recheck);
    window.addEventListener(USER_REQUIRED_EVENT, invalid);
    return () => {
      document.removeEventListener("visibilitychange", recheck);
      window.removeEventListener("focus", recheck);
      window.removeEventListener(USER_REQUIRED_EVENT, invalid);
    };
  }, [user]);

  if (pathname.replace(/\/+$/, "") === "/pair") return <>{children}</>;
  if (user === null && restoring) return <p className="user-gate-status">Cargando…</p>;
  if (user === null) {
    return (
      <ChooseContext.Provider value={choose}>
        <Selection note={note} />
      </ChooseContext.Provider>
    );
  }
  return <ActiveUserContext.Provider value={{ user, signOut, setUser }}>
      <AppBar />
      {photoNotice && (
        <p className="user-gate-note photo-notice" role="status">
          {PHOTO_NOTICE}
          <button type="button" className="photo-notice-close" onClick={() => setPhotoNotice(false)}>
            Cerrar aviso
          </button>
        </p>
      )}
      {children}
    </ActiveUserContext.Provider>;
}
