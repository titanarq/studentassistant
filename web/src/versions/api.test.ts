import { afterEach, expect, it, vi } from "vitest";
import { jsonResponse, stubApi } from "../test/mockApi";
import { fetchVersionDiff, readDiff, readRestore, readVersions, restoreVersion } from "./api";
import { section, version, versionDiff, versions } from "./testVersions";

afterEach(() => {
  vi.unstubAllGlobals();
});

const BASE = "/api/subjects/historia/topics/revolucion-francesa/notes/versions";

it("reads the versions oldest first and skips malformed entries", () => {
  const read = readVersions(versions([version(2, { current: true }), { version: 0 }, version(1, { tagged_at: 5 })]));

  expect(read?.versions.map((v) => [v.version, v.current, v.tagged_at])).toEqual([
    [1, false, null],
    [2, true, "2026-09-22T08:00:00+02:00"],
  ]);
  expect(read?.has_notes).toBe(true);
  expect(readVersions({ subject: "historia" })).toBeNull();
});

it("reads a diff by section and drops sections of an unknown status", () => {
  const read = readDiff(
    versionDiff(1, null, [section("causas", { status: "changed", moved: true }), section("x", { status: "weird" })], {
      footnotes: { added: ["p3"], removed: 7 },
    }),
  );

  expect(read?.to_version).toBeNull();
  expect(read?.sections.map((s) => [s.key, s.status, s.moved])).toEqual([["causas", "changed", true]]);
  expect(read?.footnotes).toEqual({ added: ["p3"], removed: [], changed: [] });
  expect(readDiff({ sections: [] })).toBeNull();
});

it("reads a restore result", () => {
  expect(readRestore({ restored_version: 1, version: 4, errors: ["x"], warning: null, diff: "" })).toEqual({
    restored_version: 1,
    version: 4,
    errors: ["x"],
    warning: null,
  });
  expect(readRestore({ version: 4 })).toBeNull();
});

it("asks the diff against the current notes without `to`", async () => {
  const fetchMock = stubApi({
    [`${BASE}/diff?from=2`]: jsonResponse(versionDiff(2, null, [])),
    [`${BASE}/diff?from=1&to=3`]: jsonResponse(versionDiff(1, 3, [])),
  });

  expect((await fetchVersionDiff("historia", "revolucion-francesa", 2, null)).kind).toBe("ok");
  expect((await fetchVersionDiff("historia", "revolucion-francesa", 1, 3)).kind).toBe("ok");
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it("keeps the backend's Spanish detail of a refusal", async () => {
  stubApi({
    [`${BASE}/diff?from=9`]: jsonResponse({ detail: "No existe la versión 9 de los apuntes de este tema." }, 404),
    [`POST ${BASE}/2/restore`]: jsonResponse({ detail: "Los apuntes actuales ya son la versión 2." }, 409),
  });

  expect(await fetchVersionDiff("historia", "revolucion-francesa", 9, null)).toMatchObject({
    kind: "refused",
    status: 404,
    detail: "No existe la versión 9 de los apuntes de este tema.",
  });
  expect(await restoreVersion("historia", "revolucion-francesa", 2)).toMatchObject({ kind: "refused", status: 409 });
});

it("reports an unreachable backend", async () => {
  stubApi({ [BASE + "/diff?from=1"]: new TypeError("network") });

  expect(await fetchVersionDiff("historia", "revolucion-francesa", 1, null)).toEqual({ kind: "unreachable" });
});
