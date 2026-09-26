import { useEffect, useRef, useState } from "react";
import { fetchSourceMeta, type SourceMeta } from "../../notes/api";

/** At most this many `GET /api/sources/{id}/meta` requests run at once. */
export const META_CONCURRENCY = 4;

/** Runs `tasks` with at most `limit` of them in flight. */
export async function runBounded(tasks: Array<() => Promise<void>>, limit: number): Promise<void> {
  let next = 0;
  const worker = async () => {
    while (next < tasks.length) {
      const task = tasks[next++];
      await task();
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
}

/**
 * The metadata of the listed sources (`vault_id` -> `SourceMeta`, `null` when it could not be
 * read), cached per source: a source already read is not asked for again until `reloadKey`
 * changes, and then every listed source is read again (its triage may have changed through a
 * `capture.triaged`) while the cached value stays shown. Reads run with bounded concurrency; an
 * older answer never overwrites a newer one.
 */
export function useSourceMetas(vaultIds: readonly string[], reloadKey: unknown): ReadonlyMap<string, SourceMeta | null> {
  const [metas, setMetas] = useState<ReadonlyMap<string, SourceMeta | null>>(new Map());
  /** Per source: the generation of its last request and of the answer shown. */
  const requested = useRef(new Map<string, number>());
  const applied = useRef(new Map<string, number>());
  const generation = useRef(0);
  const lastKey = useRef<{ key: unknown } | null>(null);
  const idsKey = vaultIds.join("\n");

  useEffect(() => {
    if (lastKey.current === null || !Object.is(lastKey.current.key, reloadKey)) {
      lastKey.current = { key: reloadKey };
      generation.current++;
    }
    const gen = generation.current;
    let cancelled = false;
    const ids = idsKey === "" ? [] : idsKey.split("\n");
    const due = ids.filter((id) => requested.current.get(id) !== gen);
    for (const id of due) requested.current.set(id, gen);
    void runBounded(
      due.map((id) => async () => {
        if (cancelled) return;
        const result = await fetchSourceMeta(id);
        if (cancelled || (applied.current.get(id) ?? 0) > gen) return;
        applied.current.set(id, gen);
        const value = result.kind === "ok" ? result.value : null;
        setMetas((current) => {
          const updated = new Map(current);
          updated.set(id, value);
          return updated;
        });
      }),
      META_CONCURRENCY,
    );
    return () => {
      cancelled = true;
      // Requests this run gave up on are due again in the next one.
      for (const id of due) if (applied.current.get(id) !== gen) requested.current.delete(id);
    };
  }, [idsKey, reloadKey]);

  return metas;
}
