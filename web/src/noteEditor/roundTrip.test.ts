// The round-trip corpus of the visual editor (#316): every fixture loads and saves unchanged, and
// a one-word edit in one block changes only that block.
import { createVisualEditor, tidyBlock, type VisualEditor } from "./visualEditor";

const corpus = import.meta.glob<string>("./fixtures/*.md", { query: "?raw", import: "default", eager: true });

/** Per fixture, a word of one paragraph, list item or table cell to replace. */
const EDITS: Record<string, string> = {
  "anclas.md": "interpreta",
  "dudosa.md": "pueblo",
  "ejemplo-editor.md": "fábricas",
  "fuentes-finales.md": "profesor",
  "imagen.md": "resume",
  "listas-anidadas.md": "ferrocarril",
  "referencias.md": "situación",
  "regla.md": "después",
  "tabla.md": "ventana",
};

const withoutTrailingSpace = (text: string) => text.replace(/[ \t]+$/gm, "");

async function load(text: string): Promise<[VisualEditor, HTMLElement]> {
  const root = document.createElement("div");
  document.body.append(root);
  return [await createVisualEditor(root, text), root];
}

/** Types `replacement` over the first occurrence of `word` in the document's text. */
function typeOver(editor: VisualEditor, word: string, replacement: string) {
  const view = editor.view();
  let at = -1;
  view.state.doc.descendants((node, pos) => {
    if (at >= 0) return false;
    const index = node.isText ? (node.text ?? "").indexOf(word) : -1;
    if (index >= 0) at = pos + index;
    return true;
  });
  if (at < 0) throw new Error(`${word} is not in the document`);
  view.dispatch(view.state.tr.insertText(replacement, at, at + word.length));
}

describe("visual editor round trip", () => {
  const names = Object.keys(corpus).map((path) => path.replace("./fixtures/", ""));

  it("covers the whole corpus", () => {
    expect(names.sort()).toEqual(Object.keys(EDITS).sort());
  });

  it.each(names)("%s loads and saves unchanged", async (name) => {
    const text = corpus[`./fixtures/${name}`];
    const [editor, root] = await load(text);
    expect(withoutTrailingSpace(editor.getMarkdown())).toBe(withoutTrailingSpace(text));
    await editor.destroy();
    root.remove();
  });

  it.each(names)("%s: a one-word edit changes only its block", async (name) => {
    const text = corpus[`./fixtures/${name}`];
    const word = EDITS[name];
    const [editor, root] = await load(text);
    typeOver(editor, word, "CAMBIO");
    expect(withoutTrailingSpace(editor.getMarkdown())).toBe(withoutTrailingSpace(text.replace(word, "CAMBIO")));
    await editor.destroy();
    root.remove();
  });
});

it("an edited heading keeps its anchor as written", async () => {
  const text = corpus["./fixtures/anclas.md"];
  const [editor, root] = await load(text);
  typeOver(editor, "Funciones del lenguaje", "Las funciones del lenguaje");
  expect(editor.getMarkdown()).toBe(text.replace("## 2. Funciones", "## 2. Las funciones"));
  await editor.destroy();
  root.remove();
});

describe("tidyBlock", () => {
  it("writes table delimiters as ---", () => {
    expect(tidyBlock("| a | b | c |\n| :- | :-: | -: |\n| d | e | f |")).toBe("| a | b | c |\n| --- | :---: | ---: |\n| d | e | f |");
  });

  it("drops the escapes of anchors and doubtful words", () => {
    expect(tidyBlock("## A {#a\\_b\\-c}\n\nLa \\[\\[?soberanía]] reside.")).toBe("## A {#a_b-c}\n\nLa [[?soberanía]] reside.");
  });
});
