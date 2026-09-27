import { act, createRef } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeAll, expect, it, vi } from "vitest";
import NoteEditor, { type NoteEditorHandle, type UploadResult } from "./NoteEditor";
import { PAGE_TEST_TIMEOUT } from "../test/timeouts";

const TEXT = `# Tema

## Funciones {#funciones_del_lenguaje}

Cada función se centra en un elemento.[^p3]

[^p3]: [Apuntes, página 3](../sources/notes/page-003.jpg)
`;

// The editor loads Milkdown lazily (`import("./visualEditor")`); cold, that import transforms and
// evaluates the whole Milkdown graph. Load it once here, outside any test's readiness wait.
beforeAll(async () => {
  await import("./visualEditor");
}, 30_000);

/** A bound for the editor to open on a loaded machine (it takes well under a second). */
const READY_TIMEOUT = 5_000;

function renderEditor(uploadImage = vi.fn(async (): Promise<UploadResult> => ({ kind: "failed", message: "no" }))) {
  const ref = createRef<NoteEditorHandle>();
  const onChange = vi.fn();
  render(
    <NoteEditor
      ref={ref}
      initialText={TEXT}
      uploadImage={uploadImage}
      renderPreview={(text) => <pre>{text}</pre>}
      onChange={onChange}
      resolveImage={(src) => `/img/${src.split("/").pop()}`}
    />,
  );
  return { ref, onChange, uploadImage };
}

/**
 * Waits until the visual editor is open. The probe is a cheap attribute check on the surface: a
 * `getByRole` probe costs ~150 ms in jsdom and `waitFor` runs it on every DOM mutation while
 * ProseMirror mounts, which starves the very promises it waits for (the flake of #363).
 */
async function visualReady() {
  const surface = screen.getByLabelText("Apuntes en edición");
  await waitFor(() => expect(surface).toHaveAttribute("aria-busy", "false"), { timeout: READY_TIMEOUT });
  expect(screen.getByRole("button", { name: "Negrita" })).toBeEnabled();
  return surface;
}

it("hides heading anchors in the visual editor but keeps them in the text", async () => {
  const { ref } = renderEditor();
  const surface = await visualReady();
  const hidden = surface.querySelector(".note-anchor-hidden");
  expect(hidden).toHaveTextContent("{#funciones_del_lenguaje}");
  expect(ref.current?.getText()).toBe(TEXT);
}, PAGE_TEST_TIMEOUT);

it("keeps the text across Visual and Markdown", async () => {
  const { ref } = renderEditor();
  await visualReady();
  expect(ref.current?.getText()).toBe(TEXT);
  fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
  const area = screen.getByLabelText("Apuntes en Markdown");
  expect(area).toHaveValue(TEXT);
  fireEvent.change(area, { target: { value: `${TEXT}\nOtra idea.[^p3]\n` } });
  fireEvent.click(screen.getByRole("button", { name: "Visual" }));
  await visualReady();
  expect(ref.current?.getText()).toBe(`${TEXT}\nOtra idea.[^p3]\n`);
}, PAGE_TEST_TIMEOUT);

it("inserts a table in the visual editor and saves it in the notes' style", async () => {
  const { ref, onChange } = renderEditor();
  const surface = await visualReady();
  fireEvent.click(screen.getByRole("button", { name: "Tabla" }));
  expect(within(surface).getByRole("table")).toBeInTheDocument();
  expect(onChange).toHaveBeenCalled();
  const text = ref.current?.getText() ?? "";
  expect(text).toBe(`| | |\n| --- | --- |\n| | |\n| | |\n\n${TEXT}`);
});

it("uploads a pasted image in the visual editor and shows it from the vault", async () => {
  const uploadImage = vi.fn(
    async (): Promise<UploadResult> => ({ kind: "ok", markdown: "![Imagen pegada 1](../sources/images/img-001.png)" }),
  );
  const { ref } = renderEditor(uploadImage);
  const surface = await visualReady();
  const file = new File([new Uint8Array([0x89, 0x50])], "a.png", { type: "image/png" });
  await act(async () => {
    fireEvent.paste(surface.querySelector(".ProseMirror") as Element, { clipboardData: { files: [file], items: [], types: ["Files"] } });
  });
  expect(uploadImage).toHaveBeenCalledWith(file);
  await waitFor(() => expect(within(surface).getByRole("img", { name: "Imagen pegada 1" })).toHaveAttribute("src", "/img/img-001.png"));
  expect(ref.current?.getText()).toContain("![Imagen pegada 1](../sources/images/img-001.png)");
});

it("refuses a file that is not an image it can store", async () => {
  const { uploadImage } = renderEditor();
  const surface = await visualReady();
  const file = new File(["GIF89a"], "a.gif", { type: "image/gif" });
  fireEvent.drop(surface, { dataTransfer: { files: [file], items: [], types: ["Files"] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("Solo se pueden pegar imágenes PNG, JPEG o WebP.");
  expect(uploadImage).not.toHaveBeenCalled();
});
