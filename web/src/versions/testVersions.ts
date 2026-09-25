/** Bodies of the notes versions API as the backend answers them (#64). */

export function version(n: number, extra: Record<string, unknown> = {}) {
  return {
    version: n,
    tag: `historia/revolucion-francesa/apuntes-v${n}`,
    commit: `c0ffee${n}`,
    tagged_at: `2026-09-2${n}T08:00:00+02:00`,
    message: `Apuntes v${n} de historia/revolucion-francesa`,
    current: false,
    ...extra,
  };
}

export function versions(list: unknown[], extra: Record<string, unknown> = {}) {
  return {
    subject: "historia",
    topic: "revolucion-francesa",
    versions: list,
    has_notes: true,
    changed_since_latest: false,
    ...extra,
  };
}

export function section(key: string, extra: Record<string, unknown> = {}) {
  return {
    key,
    anchor: key === "" ? null : key,
    title_before: key === "" ? "La Revolución francesa" : key,
    title_after: key === "" ? "La Revolución francesa" : key,
    level: key === "" ? 1 : 2,
    status: "unchanged",
    moved: false,
    renamed: false,
    diff: "",
    ...extra,
  };
}

export function versionDiff(from: number, to: number | null, sections: unknown[], extra: Record<string, unknown> = {}) {
  return {
    subject: "historia",
    topic: "revolucion-francesa",
    from_version: from,
    to_version: to,
    identical: false,
    sections,
    footnotes: { added: [], removed: [], changed: [] },
    diff: "",
    ...extra,
  };
}
