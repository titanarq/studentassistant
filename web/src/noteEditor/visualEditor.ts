/**
 * The visual (WYSIWYG) Markdown editor of the notes (#316): Milkdown (ProseMirror + remark,
 * CommonMark + GFM for tables and footnotes), set up so the notes format survives it:
 *
 * - every top-level block the student did not touch is saved as the text it was loaded from
 *   (`preserve.ts`), so an untouched document round-trips byte for byte;
 * - a block the student changed is serialised with the notes' own style (`-` bullets, `---`
 *   rules, unpadded tables) and without the escapes remark adds to `{#anchor}` and `[[?word]]`;
 * - provenance references (`[^p4]`, `[^ia]`, `[^est]`) are atomic chips: they can be moved or
 *   deleted but not typed into; `[^est]` shows as "tú";
 * - images without a title load (Milkdown 7.22 drops them otherwise) and show from the vault.
 */

import { defaultValueCtx, Editor, editorViewCtx, remarkCtx, remarkStringifyOptionsCtx, rootCtx, serializerCtx } from "@milkdown/core";
import { history } from "@milkdown/plugin-history";
import {
  commonmark,
  imageSchema,
  insertHrCommand,
  setBlockTypeCommand,
  headingSchema,
  paragraphSchema,
  toggleEmphasisCommand,
  toggleStrongCommand,
  wrapInBulletListCommand,
  wrapInOrderedListCommand,
} from "@milkdown/preset-commonmark";
import {
  addColAfterCommand,
  addRowAfterCommand,
  footnoteReferenceSchema,
  gfm,
  insertTableCommand,
  remarkGFMPlugin,
} from "@milkdown/preset-gfm";
import type { Node as ProseNode } from "@milkdown/prose/model";
import { Plugin } from "@milkdown/prose/state";
import type { EditorView } from "@milkdown/prose/view";
import { $prose, callCommand, insert } from "@milkdown/utils";
import { IA_LABEL } from "../notes/markdown";
import { assemble, buildSourceMap, type Positioned, type SourceMap } from "./preserve";

export const STUDENT_LABEL = "est";

/** What a provenance chip shows for a footnote label. */
export function chipText(label: string): string {
  if (label === IA_LABEL) return "IA";
  if (label === STUDENT_LABEL) return "tú";
  return label;
}

/** The accessible name of a provenance chip. */
export function chipDescription(label: string): string {
  if (label === IA_LABEL) return "Añadido por el asistente";
  if (label === STUDENT_LABEL) return "Escrito por ti";
  return `Fuente ${label}`;
}

/**
 * A block the student changed, in the notes' style: without the escapes remark adds to
 * `{#anchor}` and `[[?word]]`, and with `---` table delimiters (remark writes `-` and Milkdown
 * aligns new tables left, `:-`).
 */
export function tidyBlock(markdown: string): string {
  return markdown
    .replace(/^\|(?: *:?-+:? *\|)+ *$/gm, (row: string) =>
      // Left is the default alignment: `:---` is written `---`.
      row.replace(/(:?)-+(:?)/g, (_dashes: string, left: string, right: string) => (right === "" ? "---" : `${left}---${right}`)),
    )
    .replace(/\\\[\\\[\?([^\]\n]*?)\]\]/g, "[[?$1]]")
    .replace(/\{#([^}\n]*)\}/g, (anchor: string) => anchor.replace(/\\([_-])/g, "$1"));
}

export type BlockKind = "paragraph" | "h2" | "h3";

export interface VisualEditor {
  /** The document as Markdown, untouched blocks as they were loaded. */
  getMarkdown(): string;
  /** Inserts Markdown (a pasted image's) at the cursor. */
  insertMarkdown(markdown: string): void;
  setBlock(kind: BlockKind): void;
  toggleStrong(): void;
  toggleEmphasis(): void;
  bulletList(): void;
  orderedList(): void;
  rule(): void;
  insertTable(): void;
  addRow(): void;
  addColumn(): void;
  focus(): void;
  /** The ProseMirror view (tests type into it). */
  view(): EditorView;
  destroy(): Promise<void>;
}

export interface VisualEditorOptions {
  /** Called after every change of the document. */
  onChange?: () => void;
  /** The URL an image of the notes (`../sources/images/img-001.png`) is shown from. */
  resolveImage?: (src: string) => string;
}

const TABLE_ROWS = 3;
const TABLE_COLUMNS = 2;

export async function createVisualEditor(
  root: HTMLElement,
  markdown: string,
  { onChange, resolveImage = (src) => src }: VisualEditorOptions = {},
): Promise<VisualEditor> {
  const image = imageSchema.extendSchema((prev) => (ctx) => {
    const base = prev(ctx);
    return {
      ...base,
      toDOM: (node) => ["img", { alt: node.attrs.alt, title: node.attrs.title || null, src: resolveImage(node.attrs.src) }],
      parseMarkdown: {
        match: ({ type }) => type === "image",
        runner: (state, node, type) => {
          state.addNode(type, { src: String(node.url ?? ""), alt: String(node.alt ?? ""), title: String(node.title ?? "") });
        },
      },
    };
  });
  const reference = footnoteReferenceSchema.extendSchema((prev) => (ctx) => ({
    ...prev(ctx),
    toDOM: (node) => {
      const label = String(node.attrs.label);
      const kind = label === IA_LABEL ? "ia" : label === STUDENT_LABEL ? "est" : "source";
      return [
        "sup",
        {
          "data-type": "footnote_reference",
          "data-label": label,
          class: `note-chip note-chip-${kind}`,
          "aria-label": chipDescription(label),
          contenteditable: "false",
        },
        chipText(label),
      ];
    },
  }));

  const changes = $prose(
    () =>
      new Plugin({
        view: () => ({
          update: (view, previous) => {
            if (!view.state.doc.eq(previous.doc)) onChange?.();
          },
        }),
      }),
  );

  const editor = await Editor.make()
    .config((ctx) => {
      ctx.set(rootCtx, root);
      ctx.set(defaultValueCtx, markdown);
      ctx.update(remarkStringifyOptionsCtx, (options) => ({ ...options, bullet: "-" as const, rule: "-" as const, emphasis: "*" as const, strong: "*" as const }));
      ctx.set(remarkGFMPlugin.options.key, { tablePipeAlign: false });
    })
    .use(commonmark)
    .use(image)
    .use(gfm)
    .use(reference)
    .use(history)
    .use(changes)
    .create();

  const map: SourceMap | null = editor.action((ctx) => {
    const remark = ctx.get(remarkCtx);
    const tree = remark.runSync(remark.parse(markdown), markdown) as { children?: Positioned[] };
    const doc = ctx.get(editorViewCtx).state.doc;
    const nodes: ProseNode[] = [];
    doc.forEach((node) => nodes.push(node));
    return buildSourceMap(markdown, tree.children ?? [], nodes);
  });

  return {
    getMarkdown: () =>
      editor.action((ctx) => {
        const view = ctx.get(editorViewCtx);
        const serializer = ctx.get(serializerCtx);
        const doc = view.state.doc;
        const nodes: ProseNode[] = [];
        doc.forEach((node) => nodes.push(node));
        const one = (node: ProseNode) => tidyBlock(serializer(doc.type.create(null, node)).replace(/\n+$/, ""));
        return assemble(map, nodes, one);
      }),
    insertMarkdown: (text) => editor.action(insert(text)),
    setBlock: (kind) =>
      editor.action((ctx) => {
        const nodeType = kind === "paragraph" ? paragraphSchema.type(ctx) : headingSchema.type(ctx);
        const attrs = kind === "paragraph" ? undefined : { level: kind === "h2" ? 2 : 3 };
        callCommand(setBlockTypeCommand.key, { nodeType, attrs })(ctx);
      }),
    toggleStrong: () => editor.action(callCommand(toggleStrongCommand.key)),
    toggleEmphasis: () => editor.action(callCommand(toggleEmphasisCommand.key)),
    bulletList: () => editor.action(callCommand(wrapInBulletListCommand.key)),
    orderedList: () => editor.action(callCommand(wrapInOrderedListCommand.key)),
    rule: () => editor.action(callCommand(insertHrCommand.key)),
    insertTable: () => editor.action(callCommand(insertTableCommand.key, { row: TABLE_ROWS, col: TABLE_COLUMNS })),
    addRow: () => editor.action(callCommand(addRowAfterCommand.key)),
    addColumn: () => editor.action(callCommand(addColAfterCommand.key)),
    view: () => editor.action((ctx) => ctx.get(editorViewCtx)),
    focus: () => editor.action((ctx) => ctx.get(editorViewCtx).focus()),
    destroy: async () => {
      await editor.destroy();
    },
  };
}
