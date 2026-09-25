/**
 * The Spanish sentence for a REST call of the capture page that failed (#40). Every call
 * `capture/api.ts` makes answers the same discriminated result and every screen shows a failure the
 * same way: the sentence it was given as a prefix, then what the failure was. The `detail` of a
 * refusal is the backend's own and is Spanish already (docs/modules/server.md), so it is shown as it
 * comes; the wording of a body that broke the protocol is the decoder's and is not, so only the
 * schema's name reaches the page.
 */

import type { ApiResult } from "./api";

/** Any answer that is not the decoded body, which is what a screen turns into a sentence. */
export type ApiFailure = Exclude<ApiResult<unknown>, { kind: "ok" }>;

/** What the student reads for a call that failed: `prefix`, then the reason. */
export function describeFailure(prefix: string, failure: ApiFailure): string {
  switch (failure.kind) {
    case "refused":
      return `${prefix}: ${failure.detail}`;
    case "error":
      return `${prefix}: el servidor respondió con un error (${failure.status}).`;
    case "unexpected":
      return `${prefix}: la respuesta del servidor no sigue el protocolo esperado (${failure.expected}).`;
    case "unreachable":
      return `${prefix}: no se pudo conectar con el servidor.`;
  }
}
