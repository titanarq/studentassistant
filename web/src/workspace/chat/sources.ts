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
  if (n !== null && kind === "images") return `imagen pegada ${n}`;
  return sourceItem(sourceId)?.title ?? file;
}

/** «imagen recortada 2»: an image the editor cropped from a page (#493). */
export function croppedImageName(sourceId: string): string {
  const match = NUMBER.exec(sourceId.split("/").at(-1) ?? "");
  return match === null ? "imagen recortada" : `imagen recortada ${Number(match[1])}`;
}

/** Capitalized, for the start of a line. */
export const capitalized = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);
