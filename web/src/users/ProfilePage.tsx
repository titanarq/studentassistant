import { type FormEvent, useState } from "react";
import { USER_EMAIL_MAX_CHARS, USER_EMAIL_PATTERN, USER_NAME_MAX_CHARS, type UserUpdateRequest } from "../protocol";
import { updateUser } from "./api";
import Avatar from "./Avatar";
import { useActiveUser } from "./UserGate";
import "./profilePage.css";

/** Client-side mirror of the protocol rules; the message sits next to its field. */
export function validateName(raw: string): string | null {
  const name = raw.trim();
  if (name === "") return "El nombre es obligatorio";
  if ([...name].length > USER_NAME_MAX_CHARS) return `El nombre no puede pasar de ${USER_NAME_MAX_CHARS} caracteres`;
  return null;
}

export function validateEmail(raw: string): string | null {
  const email = raw.trim();
  if (email === "") return null;
  if ([...email].length > USER_EMAIL_MAX_CHARS) return `El correo no puede pasar de ${USER_EMAIL_MAX_CHARS} caracteres`;
  if (!USER_EMAIL_PATTERN.test(email)) return "El correo electrónico no es válido";
  return null;
}

function goBack(): void {
  if (window.history.length > 1) window.history.back();
  else window.location.assign("/");
}

/** «Editar perfil»: name and email of the active user (the id is never shown as a field). */
export default function ProfilePage() {
  const { user } = useActiveUser();
  const [name, setName] = useState(user.name);
  const [email, setEmail] = useState(user.email ?? "");
  const [nameError, setNameError] = useState<string | null>(null);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setSaved(false);
    setFailure(null);
    const nError = validateName(name);
    const eError = validateEmail(email);
    setNameError(nError);
    setEmailError(eError);
    if (nError !== null || eError !== null) return;
    const changes: UserUpdateRequest = {};
    if (name.trim() !== user.name) changes.name = name.trim();
    if (email.trim() !== (user.email ?? "")) changes.email = email.trim();
    if (Object.keys(changes).length === 0) {
      setSaved(true);
      return;
    }
    setSaving(true);
    const result = await updateUser(user.id, changes);
    setSaving(false);
    if (result.kind === "ok") {
      setSaved(true);
      setName(result.user.name);
      setEmail(result.user.email ?? "");
    } else if (result.kind === "rejected") {
      setFailure(result.detail ?? "No se pudo guardar el perfil.");
    } else {
      setFailure("No se pudo conectar con el servidor.");
    }
  };

  return (
    <main className="profile-page">
      <h1 className="profile-title">Editar perfil</h1>
      <div className="profile-avatar">
        <Avatar user={user} />
      </div>
      <form className="profile-form" onSubmit={save} noValidate>
        <label className="profile-field">
          <span>Nombre</span>
          <input
            type="text"
            value={name}
            onChange={(event) => setName(event.target.value)}
            aria-invalid={nameError !== null}
            aria-describedby={nameError !== null ? "profile-name-error" : undefined}
            required
          />
          {nameError !== null && (
            <small id="profile-name-error" className="profile-error" role="alert">
              {nameError}
            </small>
          )}
        </label>
        <label className="profile-field">
          <span>Correo electrónico</span>
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            aria-invalid={emailError !== null}
            aria-describedby={emailError !== null ? "profile-email-error" : undefined}
          />
          {emailError !== null && (
            <small id="profile-email-error" className="profile-error" role="alert">
              {emailError}
            </small>
          )}
        </label>
        {failure !== null && (
          <p className="profile-error" role="alert">
            {failure}
          </p>
        )}
        {saved && (
          <p className="profile-saved" role="status">
            Perfil guardado
          </p>
        )}
        <div className="profile-actions">
          <button type="submit" className="profile-save" disabled={saving}>
            Guardar
          </button>
          <button type="button" className="profile-cancel" onClick={goBack}>
            Cancelar
          </button>
        </div>
      </form>
    </main>
  );
}
