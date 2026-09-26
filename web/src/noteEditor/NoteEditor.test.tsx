import { act, createRef } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import NoteEditor, { type NoteEditorHandle, type UploadResult } from "./NoteEditor";

const TEXT = `# Tema

## Funciones {#funciones_del_lenguaje}

Cada función se centra en un elemento.[^p3]

[^p3]: [Apuntes, página 3](../sources/notes/page-003.jpg)
`;

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

async function visualReady() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Negrita" })).toBeEnabled());
  return screen.getByLabelText("Apuntes en edición");
}

it("hides heading anchors in the visual editor but keeps them in the text", async () => {
  const { ref } = renderEditor();
  const surface = await visualReady();
  const hidden = surface.querySelector(".note-anchor-hidden");
  expect(hidden).toHaveTextContent("{#funciones_del_lenguaje}");
  expect(ref.current?.getText()).toBe(TEXT);
});

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
});

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
