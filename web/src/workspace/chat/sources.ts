/**
 * How the workspace chat names the topic's sources (#329): the short Spanish name of a source id
 * (topic-relative, `sources/notes/page-003.jpg`) for "Incorporadas: página 3, página 4" and the
 * doubts' options, and what opens it in **Recursos** (`sourceItem`, the tab's own item).
 */

import { sourceItem } from "../resources";

const NUMBER = /^(?:page|img)-(\d+)\./;

/** «página 3», «página 83 del libro», «imagen pegada 1», else the tab's title of the source. */
export function sourceName(sourceId: string): string {
  const parts = sourceId.split("/");
  const kind = parts.at(-2);
  const file = parts.at(-1) ?? sourceId;
  const match = NUMBER.exec(file);
  const n = match === null ? null : Number(match[1]);
  if (n !== null && kind === "notes") return `página ${n}`;
  if (n !== null && kind === "book") return `página ${n} del libro`;
  if (n !== null && kind === "images") return isDiagram(file) ? `diagrama ${n}` : `imagen pegada ${n}`;
  return sourceItem(sourceId)?.title ?? file;
}

/** Whether an `images` source is an SVG diagram the editor drew (#511): only those are `.svg`. */
export function isDiagram(sourceId: string): boolean {
  return /\.svg$/i.test(sourceId);
}

/**
 * «imagen recortada 2»: an image the editor cropped from a page (#493); «diagrama 3» for an SVG
 * diagram it drew (#511), which a turn reports the same way.
 */
export function croppedImageName(sourceId: string): string {
  const match = NUMBER.exec(sourceId.split("/").at(-1) ?? "");
  const name = isDiagram(sourceId) ? "diagrama" : "imagen recortada";
  return match === null ? name : `${name} ${Number(match[1])}`;
}

/** Capitalized, for the start of a line. */
export const capitalized = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);
