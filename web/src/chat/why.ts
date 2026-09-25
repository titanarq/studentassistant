import type { Block } from "../notes/markdown";

/** The longest excerpt of a block quoted in a "¿por qué pusiste esto?" question. */
export const EXCERPT_CHARS = 280;

function blockText(block: Block): string {
  switch (block.type) {
    case "heading":
    case "paragraph":
    case "code":
      return block.text;
    case "list":
      return block.items.map((item) => item.map(blockText).join(" ")).join("; ");
    case "table":
      return [block.header, ...block.rows].map((row) => row.join(" | ")).join("; ");
    case "quote":
      return block.blocks.map(blockText).join(" ");
    case "rule":
      return "";
  }
}

/** A block's text as the student reads it: no footnote references nor doubtful-word marks. */
export function blockExcerpt(block: Block): string {
  const plain = blockText(block)
    .replace(/\[\^[^\]\s]+\]/g, "")
    .replace(/\[\[\?([^\]]*)\]\]/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length <= EXCERPT_CHARS ? plain : `${plain.slice(0, EXCERPT_CHARS - 1).trimEnd()}…`;
}

/**
 * The chat message a block's "¿Por qué pusiste esto?" sends: the question, the block quoted and
 * the section it sits in, so the editor can look at the block's cited sources and explain it.
 * `null` for a block with no text (a rule).
 */
export function whyQuestion(block: Block, section: string | null): string | null {
  const excerpt = blockExcerpt(block);
  if (excerpt === "") return null;
  const where = section !== null ? ` (en la sección #${section})` : "";
  return `¿Por qué pusiste esto?${where} «${excerpt}»`;
}
