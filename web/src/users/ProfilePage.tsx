import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";
import {
  USER_EMAIL_MAX_CHARS,
  USER_EMAIL_PATTERN,
  USER_NAME_MAX_CHARS,
  USER_PHOTO_CONTENT_TYPES,
  type UserUpdateRequest,
} from "../protocol";
import { useConfirm } from "../ui/ConfirmDialog";
import { deleteUserPhoto, updateUser, uploadUserPhoto, type UserWriteResult } from "./api";
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

export const PHOTO_MAX_BYTES = 5 * 1024 * 1024;

const PHOTO_STATUS_MESSAGES: Record<number, string> = {
  413: "La foto es demasiado grande (máximo 5 MiB).",
  415: "Formato de foto no admitido. Usa JPG, PNG o WebP.",
  422: "No se pudo leer la foto.",
};

function goBack(): void {
  if (window.history.length > 1) window.history.back();
  else window.location.assign("/");
}

/** «Editar perfil»: name and email of the active user (the id is never shown as a field). */
export default function ProfilePage() {
  const { user, setUser, signOut } = useActiveUser();
  const confirm = useConfirm();
  const fileInput = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [photoError, setPhotoError] = useState<string | null>(null);
  const [photoBusy, setPhotoBusy] = useState(false);
  const [name, setName] = useState(user.name);
  const [email, setEmail] = useState(user.email ?? "");
  const [nameError, setNameError] = useState<string | null>(null);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(
    () => () => {
      if (preview !== null) URL.revokeObjectURL(preview);
    },
    [preview],
  );

  const photoFailure = (result: UserWriteResult): string =>
    result.kind === "rejected"
      ? (result.detail ?? PHOTO_STATUS_MESSAGES[result.status] ?? "No se pudo guardar la foto.")
      : "No se pudo conectar con el servidor.";

  const pickPhoto = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file === undefined) return;
    setPhotoError(null);
    setSaved(false);
    if (!(USER_PHOTO_CONTENT_TYPES as readonly string[]).includes(file.type)) {
      setPhotoError("Formato de foto no admitido. Usa JPG, PNG o WebP.");
      return;
    }
    if (file.size > PHOTO_MAX_BYTES) {
      setPhotoError("La foto es demasiado grande (máximo 5 MiB).");
      return;
    }
    setPreview(URL.createObjectURL(file));
    setPhotoBusy(true);
    const result = await uploadUserPhoto(user.id, file);
    setPhotoBusy(false);
    setPreview(null);
    if (result.kind === "ok") setUser(result.user);
    else setPhotoError(photoFailure(result));
  };

  const removePhoto = async () => {
    setPhotoError(null);
    setSaved(false);
    const confirmed = await confirm({
      title: "¿Quitar la foto?",
      message: "Se mostrarán tus iniciales en su lugar.",
      confirmLabel: "Quitar foto",
      destructive: true,
      onConfirm: async () => {
        const result = await deleteUserPhoto(user.id);
        if (result.kind === "ok") {
          setUser(result.user);
          return null;
        }
        return photoFailure(result);
      },
    });
    void confirmed;
  };

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
      setUser(result.user);
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
        {preview !== null ? <img className="user-avatar user-avatar-md" src={preview} alt="" /> : <Avatar user={user} />}
        <div className="profile-photo-actions">
          <input
            ref={fileInput}
            type="file"
            className="profile-photo-input"
            accept={USER_PHOTO_CONTENT_TYPES.join(",")}
            aria-label="Elegir foto"
            onChange={pickPhoto}
          />
          <button type="button" className="profile-cancel" disabled={photoBusy} onClick={() => fileInput.current?.click()}>
            Cambiar foto
          </button>
          {user.photo_url && (
            <button type="button" className="profile-cancel" disabled={photoBusy} onClick={removePhoto}>
              Quitar foto
            </button>
          )}
        </div>
        {photoError !== null && (
          <p className="profile-error" role="alert">
            {photoError}
          </p>
        )}
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
          <button type="button" className="profile-cancel profile-switch" onClick={signOut}>
            Cambiar de perfil
          </button>
        </div>
      </form>
    </main>
  );
}
