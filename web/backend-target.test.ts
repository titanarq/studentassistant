import { describe, expect, it } from "vitest";
import { backendTarget } from "./backend-target.ts";

describe("backendTarget", () => {
  it("defaults to the backend's default port", () => {
    expect(backendTarget({})).toBe("http://127.0.0.1:8765");
  });

  it("follows SA_SERVER_PORT when it is set", () => {
    expect(backendTarget({ SA_SERVER_PORT: "9001" })).toBe("http://127.0.0.1:9001");
  });
});
