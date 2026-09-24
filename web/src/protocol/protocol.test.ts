// @vitest-environment node
// Pure data tests: the Node environment keeps `import.meta.url` a file URL for the examples helper.

import { describe, expect, it } from "vitest";
import { EXAMPLES_DIR, sharedExamples } from "../test/protocolExamples";
import {
  checkCompatible,
  type ClientEvent,
  DECODERS,
  IncompatibleProtocolVersionError,
  isMessageName,
  negotiate,
  parseClientEvent,
  parseMessage,
  parseServerEvent,
  parseVersion,
  PROTOCOL_VERSION,
  ProtocolDecodeError,
  type ServerEvent,
} from "./index";

const examples = sharedExamples();

describe("shared examples", () => {
  it("are read from the repository's protocol/examples directory", () => {
    expect(EXAMPLES_DIR.replaceAll("\\", "/")).toMatch(/\/protocol\/examples\/$/);
    expect(EXAMPLES_DIR.replaceAll("\\", "/")).not.toMatch(/\/web\//);
    expect(examples.length).toBeGreaterThan(0);
  });

  it("are each covered by a registered parser", () => {
    const uncovered = examples.map((e) => e.name).filter((name) => !isMessageName(name));
    expect(uncovered).toEqual([]);
  });

  it("cover every registered parser", () => {
    const names = new Set(examples.map((e) => e.name));
    expect(Object.keys(DECODERS).filter((name) => !names.has(name))).toEqual([]);
  });

  it.each(examples)("$name round-trips without loss", ({ name, json }) => {
    if (!isMessageName(name)) throw new Error(`no TypeScript parser registered for ${name}`);
    const parsed = parseMessage(name, json);
    expect(JSON.parse(JSON.stringify(parsed))).toStrictEqual(json);
  });

  it.each(examples.filter((e) => e.name.startsWith("client.")))(
    "$name parses as the ClientEvent with its wire type",
    ({ json }) => {
      const event = parseClientEvent(json);
      expect(event.type).toBe((json as { type: string }).type);
      expect(event).toStrictEqual(json);
    },
  );

  it.each(examples.filter((e) => e.name.startsWith("server.")))(
    "$name parses as the ServerEvent with its wire type",
    ({ json }) => {
      const event = parseServerEvent(json);
      expect(event.type).toBe((json as { type: string }).type);
      expect(event).toStrictEqual(json);
    },
  );
});

describe("decoders", () => {
  it("refuse an unknown or missing type", () => {
    expect(() => parseServerEvent({ type: "nope", server_time_ms: 1 })).toThrow(ProtocolDecodeError);
    expect(() => parseClientEvent({ client_time_ms: 1 })).toThrow(/unknown or missing message type/);
  });

  it("refuse an unknown field", () => {
    expect(() => parseServerEvent({ type: "notice", pending_count: 1, server_time_ms: 1, x: 1 })).toThrow(
      /x: unknown field/,
    );
  });

  it("enforce the cross-field rules of the schemas", () => {
    expect(() => parseClientEvent({ type: "button", button: "switch_source", client_time_ms: 1 })).toThrow(
      /source is required/,
    );
    expect(() => parseServerEvent({ type: "ack", server_time_ms: 1 })).toThrow(/audio_seq, capture_ids/);
    expect(() =>
      parseServerEvent({ type: "hello.ack", protocol_version: "1.0", stt_mode: "server", clock_offset_ms: 0, server_time_ms: 1 }),
    ).toThrow(/audio_format is required/);
  });

  it("refuse null for an optional field, like the schemas", () => {
    expect(() => parseClientEvent({ type: "marker", client_time_ms: 1, label: null })).toThrow(/label/);
  });
});

describe("protocol_version", () => {
  it("is 1.0", () => {
    expect(PROTOCOL_VERSION).toBe("1.0");
    expect(parseVersion(PROTOCOL_VERSION)).toEqual([1, 0]);
  });

  it("accepts the same MAJOR and refuses another one naming both versions", () => {
    expect(() => checkCompatible("1.7")).not.toThrow();
    expect(() => checkCompatible("2.0")).toThrow(IncompatibleProtocolVersionError);
    expect(() => checkCompatible("2.0")).toThrow(
      "incompatible protocol_version 2.0: this side speaks 1.0; update the older side so both share MAJOR version 1",
    );
  });

  it("negotiates the lower MINOR and rejects malformed versions", () => {
    expect(negotiate("1.3", "1.1")).toBe("1.1");
    expect(negotiate("1.0", "1.2")).toBe("1.0");
    expect(() => parseVersion("1")).toThrow(/malformed/);
    expect(() => parseVersion("01.0")).toThrow(/malformed/);
  });
});

// Exhaustiveness: `tsc` (run by `npm test`) fails if a union member loses its `case`, or if a
// member is added to a union without one.
function assertNever(value: never): never {
  throw new Error(`unhandled event ${JSON.stringify(value)}`);
}

function describeServerEvent(event: ServerEvent): string {
  switch (event.type) {
    case "hello.ack":
      return `version ${event.protocol_version}, stt ${event.stt_mode}`;
    case "transcript.partial":
    case "transcript.final":
      return event.text;
    case "command":
      return event.command;
    case "notice":
      return `${event.pending_count} pending`;
    case "ack":
      return `ack ${event.audio_seq ?? ""} ${event.capture_ids?.join(",") ?? ""}`;
    default:
      return assertNever(event);
  }
}

function describeClientEvent(event: ClientEvent): string {
  switch (event.type) {
    case "hello":
      return event.capabilities.stt_provider;
    case "transcript.client.partial":
    case "transcript.client.final":
      return event.text;
    case "button":
      return event.button;
    case "marker":
      return event.label ?? "";
    case "ack":
      return event.command_id;
    default:
      return assertNever(event);
  }
}

describe("narrowing", () => {
  it("switches over every ServerEvent and ClientEvent type", () => {
    for (const { name, json } of examples) {
      if (name.startsWith("server.")) expect(typeof describeServerEvent(parseServerEvent(json))).toBe("string");
      if (name.startsWith("client.")) expect(typeof describeClientEvent(parseClientEvent(json))).toBe("string");
    }
  });
});
