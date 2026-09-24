// Where the Vite dev server forwards `/api` and `/ws`: the local backend, on the port
// `studentassistant.config` uses (`server.port`, default 8765, overridable with SA_SERVER_PORT).

export const DEFAULT_BACKEND_PORT = 8765;

export function backendTarget(env: Record<string, string | undefined>): string {
  return `http://127.0.0.1:${env.SA_SERVER_PORT ?? DEFAULT_BACKEND_PORT}`;
}
