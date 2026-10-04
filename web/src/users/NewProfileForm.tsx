import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";
import { USER_PHOTO_CONTENT_TYPES, type User } from "../protocol";
import { createUser, uploadUserPhoto } from "./api";
import { PHOTO_MAX_BYTES, validateEmail, validateName } from "./ProfilePage";
import "./profilePage.css";

/**
 * «Nuevo perfil»: name, optional email and optional photo of a user that does not exist yet. The
 * profile is created first (`POST /api/users`) and the photo goes up right after; whoever is
 * created is handed to `onCreated`, which chooses it.
 */
export default function NewProfileForm({ onCreated, onCancel }: { onCreated: (user: User) => void; onCancel: () => void }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [photo, setPhoto] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [photoError, setPhotoError] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(
    () => () => {
      if (preview !== null) URL.revokeObjectURL(preview);
    },
    [preview],
  );

  const pickPhoto = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file === undefined) return;
    setPhotoError(null);
    if (!(USER_PHOTO_CONTENT_TYPES as readonly string[]).includes(file.type)) {
      setPhotoError("Formato de foto no admitido. Usa JPG, PNG o WebP.");
      return;
    }
    if (file.size > PHOTO_MAX_BYTES) {
      setPhotoError("La foto es demasiado grande (máximo 5 MiB).");
      return;
    }
    setPhoto(file);
    setPreview(URL.createObjectURL(file));
  };

  const removePhoto = () => {
    setPhoto(null);
    setPreview(null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setFailure(null);
    const nError = validateName(name);
    const eError = validateEmail(email);
    setNameError(nError);
    setEmailError(eError);
    if (nError !== null || eError !== null) return;
    setSaving(true);
    const trimmedEmail = email.trim();
    const result = await createUser(trimmedEmail === "" ? { name: name.trim() } : { name: name.trim(), email: trimmedEmail });
    if (result.kind !== "ok") {
      setSaving(false);
      setFailure(
        result.kind === "rejected" ? (result.detail ?? "No se pudo crear el perfil.") : "No se pudo conectar con el servidor.",
      );
      return;
    }
    // The profile exists now: a photo that fails to upload is fixed later from «Editar perfil».
    const uploaded = photo === null ? result : await uploadUserPhoto(result.user.id, photo);
    setSaving(false);
    onCreated(uploaded.kind === "ok" ? uploaded.user : result.user);
  };

  return (
    <form className="profile-form new-profile-form" onSubmit={submit} noValidate aria-label="Nuevo perfil">
      <div className="profile-avatar">
        {preview !== null ? <img className="user-avatar user-avatar-md" src={preview} alt="" /> : null}
        <div className="profile-photo-actions">
          <input
            ref={fileInput}
            type="file"
            className="profile-photo-input"
            accept={USER_PHOTO_CONTENT_TYPES.join(",")}
            aria-label="Elegir foto"
            onChange={pickPhoto}
          />
          <button type="button" className="profile-cancel" disabled={saving} onClick={() => fileInput.current?.click()}>
            {photo === null ? "Añadir foto" : "Cambiar foto"}
          </button>
          {photo !== null && (
            <button type="button" className="profile-cancel" disabled={saving} onClick={removePhoto}>
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
      <label className="profile-field">
        <span>Nombre</span>
        <input
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          aria-invalid={nameError !== null}
          aria-describedby={nameError !== null ? "new-profile-name-error" : undefined}
          required
        />
        {nameError !== null && (
          <small id="new-profile-name-error" className="profile-error" role="alert">
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
          aria-describedby={emailError !== null ? "new-profile-email-error" : undefined}
        />
        {emailError !== null && (
          <small id="new-profile-email-error" className="profile-error" role="alert">
            {emailError}
          </small>
        )}
      </label>
      {failure !== null && (
        <p className="profile-error" role="alert">
          {failure}
        </p>
      )}
      <div className="profile-actions">
        <button type="submit" className="profile-save" disabled={saving}>
          Crear perfil
        </button>
        <button type="button" className="profile-cancel" disabled={saving} onClick={onCancel}>
          Cancelar
        </button>
      </div>
    </form>
  );
}
