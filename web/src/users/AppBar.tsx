import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { useConfirm } from "../ui/ConfirmDialog";
import Avatar from "./Avatar";
import { useActiveUser } from "./UserGate";

/**
 * The slim bar above every gated page (epic #544): the avatar button at the right opens the menu
 * with «Editar perfil» and «Cerrar sesión». While mounted it publishes its height as
 * `--app-bar-height` on the root, so the pages that size themselves to the viewport subtract it.
 */
export default function AppBar() {
  const { user, signOut } = useActiveUser();
  const confirm = useConfirm();
  const barRef = useRef<HTMLElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const bar = barRef.current;
    if (bar === null) return;
    const root = document.documentElement;
    const publish = () => root.style.setProperty("--app-bar-height", `${bar.offsetHeight}px`);
    publish();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(publish);
    observer?.observe(bar);
    return () => {
      observer?.disconnect();
      root.style.removeProperty("--app-bar-height");
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    itemRefs.current[0]?.focus();
    const away = (event: MouseEvent) => {
      if (!barRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const close = () => {
    setOpen(false);
    buttonRef.current?.focus();
  };

  const onMenuKey = (event: KeyboardEvent) => {
    const items = itemRefs.current.filter((item): item is HTMLButtonElement => item !== null);
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(at + 1) % items.length]?.focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(at - 1 + items.length) % items.length]?.focus();
    } else if (event.key === "Home") {
      event.preventDefault();
      items[0]?.focus();
    } else if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1]?.focus();
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  };

  const editProfile = () => {
    setOpen(false);
    window.location.assign("/profile");
  };

  const askSignOut = async () => {
    setOpen(false);
    const confirmed = await confirm({
      title: `¿Cerrar la sesión de ${user.name}?`,
      confirmLabel: "Cerrar sesión",
    });
    if (confirmed) signOut();
    else buttonRef.current?.focus();
  };

  return (
    <header className="app-bar" ref={barRef}>
      <div className="app-bar-user">
        <button
          type="button"
          ref={buttonRef}
          className="app-bar-avatar"
          aria-label={`Menú de ${user.name}`}
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setOpen(true);
            }
          }}
        >
          <Avatar user={user} size="sm" />
        </button>
        {open && (
          <div className="app-bar-menu" role="menu" aria-label={`Menú de ${user.name}`} onKeyDown={onMenuKey}>
            <button type="button" role="menuitem" ref={(el) => { itemRefs.current[0] = el; }} onClick={editProfile}>
              Editar perfil
            </button>
            <button type="button" role="menuitem" ref={(el) => { itemRefs.current[1] = el; }} onClick={() => void askSignOut()}>
              Cerrar sesión
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
