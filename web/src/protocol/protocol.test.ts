// @vitest-environment node
// Pure data tests: the Node environment keeps `import.meta.url` a file URL for the examples helper.

import { describe, expect, it } from "vitest";
import { EXAMPLES_DIR, sharedExamples } from "../test/protocolExamples";
import {
  checkCompatible,
  type ClientEvent,
  DECODERS,
  DIGEST_EXCERPT_MAX,
  ERROR_CODE_SINCE,
  ERROR_CODES,
  ERROR_CODES_SINCE,
  errorCode,
  IncompatibleProtocolVersionError,
  isErrorCode,
  isMessageName,
  type MessageName,
  negotiate,
  parseClientEvent,
  parseMessage,
  parseServerEvent,
  parseVersion,
  PROTOCOL_VERSION,
  ProtocolDecodeError,
  type ServerEvent,
  STT_STATUS_DETAIL_MAX_CHARS,
  USER_COOKIE,
  USER_EMAIL_MAX_CHARS,
  USER_ERROR_CODES_SINCE,
  USER_HEADER,
  USER_NAME_MAX_CHARS,
  USER_PHOTO_CONTENT_TYPES,
  VOCABULARY_HINT_MAX_CHARS,
  VOCABULARY_HINTS_MAX_ITEMS,
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
    expect(() => parseClientEvent({ type: "button", button: "resume", reason: "student", client_time_ms: 1 })).toThrow(
      /reason is only allowed with pause/,
    );
    expect(parseClientEvent({ type: "button", button: "pause", reason: "student", client_time_ms: 1 })).toEqual({
      type: "button",
      button: "pause",
      reason: "student",
      client_time_ms: 1,
    });
    expect(() => parseServerEvent({ type: "ack", server_time_ms: 1 })).toThrow(/audio_seq, capture_ids/);
    expect(() =>
      parseServerEvent({ type: "hello.ack", protocol_version: "1.0", stt_mode: "server", clock_offset_ms: 0, server_time_ms: 1 }),
    ).toThrow(/audio_format is required/);
  });

  it("refuse null for an optional field, like the schemas", () => {
    expect(() => parseClientEvent({ type: "marker", client_time_ms: 1, label: null })).toThrow(/label/);
  });
});

describe("topic digest excerpt", () => {
  const topic = { topic_id: "t1", subject_id: "s1", name: "Tema 1" };

  it("is optional and bounded to DIGEST_EXCERPT_MAX", () => {
    expect(parseMessage("rest.topics.create.response", topic)).toStrictEqual(topic);
    const excerpt = "x".repeat(DIGEST_EXCERPT_MAX);
    expect(parseMessage("rest.topics.create.response", { ...topic, digest_excerpt: excerpt })).toStrictEqual({
      ...topic,
      digest_excerpt: excerpt,
    });
    expect(() => parseMessage("rest.topics.create.response", { ...topic, digest_excerpt: `${excerpt}x` })).toThrow(
      /digest_excerpt/,
    );
    expect(() => parseMessage("rest.topics.create.response", { ...topic, digest_excerpt: "" })).toThrow(
      /digest_excerpt/,
    );
  });
});

describe("vocabulary hints", () => {
  const notice = { type: "notice", pending_count: 0, server_time_ms: 1 };

  it("are optional and bounded in count and length", () => {
    expect(parseMessage("server.notice", notice)).toStrictEqual(notice);
    const hints = Array.from({ length: VOCABULARY_HINTS_MAX_ITEMS }, (_, i) => `t${i}`);
    expect(parseMessage("server.notice", { ...notice, vocabulary_hints: hints })).toStrictEqual({
      ...notice,
      vocabulary_hints: hints,
    });
    for (const bad of [[], [...hints, "x"], [""], ["x".repeat(VOCABULARY_HINT_MAX_CHARS + 1)]]) {
      expect(() => parseMessage("server.notice", { ...notice, vocabulary_hints: bad })).toThrow(/vocabulary_hints/);
    }
  });
});

describe("stt.status", () => {
  const status = { type: "stt.status", state: "ok", server_time_ms: 1 };

  it("has a known state and an optional bounded detail", () => {
    expect(parseMessage("server.stt.status", status)).toStrictEqual(status);
    const degraded = { ...status, state: "unavailable", detail: "x".repeat(STT_STATUS_DETAIL_MAX_CHARS) };
    expect(parseMessage("server.stt.status", degraded)).toStrictEqual(degraded);
    expect(() => parseMessage("server.stt.status", { ...status, state: "idle" })).toThrow(/state/);
    for (const bad of ["", "x".repeat(STT_STATUS_DETAIL_MAX_CHARS + 1)]) {
      expect(() => parseMessage("server.stt.status", { ...status, detail: bad })).toThrow(/detail/);
    }
  });
});

// The users bodies of protocol 1.8 (#545). `shared examples` above round-trips the five examples;
// this pins what this side refuses, field by field, exactly as the schemas and the Python models do.
describe("users (1.8)", () => {
  const user = { id: "laura-mendez", name: "Laura Méndez" };

  function refused(message: MessageName, body: unknown): void {
    expect(() => parseMessage(message, body)).toThrow(ProtocolDecodeError);
  }

  it("refuse a User with an unknown field, in a response and inside the list", () => {
    const tampered = { ...user, role: "admin" };
    expect(() => parseMessage("rest.users.create.response", tampered)).toThrow(/role: unknown field/);
    refused("rest.users.update.response", tampered);
    refused("rest.users.list.response", { users: [tampered] });
  });

  it.each(["", " ", "   ", "\t"])("refuse the blank name %j", (blank) => {
    refused("rest.users.create.request", { name: blank });
    refused("rest.users.update.request", { name: blank });
    refused("rest.users.create.response", { ...user, name: blank });
    refused("rest.users.list.response", { users: [{ ...user, name: blank }] });
  });

  it.each([" Laura", "Laura ", " Laura Méndez "])("refuse the untrimmed name %j, which travels trimmed", (name) => {
    refused("rest.users.create.response", { ...user, name });
  });

  it("bounds a name to USER_NAME_MAX_CHARS", () => {
    const longest = "a".repeat(USER_NAME_MAX_CHARS);
    expect(parseMessage("rest.users.create.response", { ...user, name: longest })).toStrictEqual({
      ...user,
      name: longest,
    });
    refused("rest.users.create.response", { ...user, name: `${longest}a` });
  });

  it("bounds an email to USER_EMAIL_MAX_CHARS", () => {
    const longest = `${"a".repeat(USER_EMAIL_MAX_CHARS - "@example.com".length)}@example.com`;
    expect(parseMessage("rest.users.create.response", { ...user, email: longest })).toStrictEqual({
      ...user,
      email: longest,
    });
    refused("rest.users.create.response", { ...user, email: `a${longest}` });
  });

  it.each(["laura@example", "laura.example.com", "laura@ example.com", "laura@@example.com", "@example.com"])(
    "refuse the malformed email %j",
    (email) => {
      refused("rest.users.create.response", { ...user, email });
      refused("rest.users.create.request", { name: "Laura Méndez", email });
    },
  );

  it("takes an empty email only in the update request, where it clears the one the user has", () => {
    expect(parseMessage("rest.users.update.request", { email: "" })).toStrictEqual({ email: "" });
    refused("rest.users.create.request", { name: "Laura Méndez", email: "" });
    refused("rest.users.create.response", { ...user, email: "" });
    refused("rest.users.list.response", { users: [{ ...user, email: "" }] });
  });

  it.each(["email", "photo_url"])("refuse null for %s, which is absent when the user has none", (field) => {
    refused("rest.users.create.response", { ...user, [field]: null });
    expect(parseMessage("rest.users.create.response", user)).toStrictEqual(user);
  });

  it("refuse an update carrying neither field, and take either one on its own", () => {
    expect(() => parseMessage("rest.users.update.request", {})).toThrow(/at least one/);
    expect(parseMessage("rest.users.update.request", { name: "Laura Méndez Ruiz" })).toStrictEqual({
      name: "Laura Méndez Ruiz",
    });
    expect(parseMessage("rest.users.update.request", { email: "laura.mendez@example.com" })).toStrictEqual({
      email: "laura.mendez@example.com",
    });
  });

  it.each(["/api/users/laura-mendez/photo", "/api/users/laura-mendez"])("take the photo_url %j", (photo_url) => {
    expect(parseMessage("rest.users.create.response", { ...user, photo_url })).toStrictEqual({ ...user, photo_url });
  });

  it.each(["https://example.com/photo.jpg", "/api/subjects/biologia", "api/users/laura-mendez/photo"])(
    "refuse the photo_url %j, which is not a path of the users API",
    (photo_url) => {
      refused("rest.users.create.response", { ...user, photo_url });
    },
  );

  it.each(["laura-mendez", "laura-mendez-2", "L2"])("take the user id %j, an id like any other", (userId) => {
    expect(parseMessage("rest.users.create.response", { ...user, id: userId })).toStrictEqual({ ...user, id: userId });
  });

  it.each(["Laura Méndez", "-laura", "laura.mendez", ""])("refuse the id %j, which is not a slug", (userId) => {
    refused("rest.users.create.response", { ...user, id: userId });
  });

  it("names the header, the cookie and the photo types of the active-user rule", () => {
    expect(USER_HEADER).toBe("X-SA-User");
    expect(USER_COOKIE).toBe("sa_user");
    expect(USER_NAME_MAX_CHARS).toBe(80);
    expect(USER_EMAIL_MAX_CHARS).toBe(254);
    expect(USER_PHOTO_CONTENT_TYPES).toEqual(["image/jpeg", "image/png", "image/webp"]);
  });
});

describe("protocol_version", () => {
  it("is 1.8", () => {
    expect(PROTOCOL_VERSION).toBe("1.8");
    expect(parseVersion(PROTOCOL_VERSION)).toEqual([1, 8]);
  });

  it("accepts the same MAJOR and refuses another one naming both versions", () => {
    expect(() => checkCompatible("1.8")).not.toThrow();
    expect(() => checkCompatible("2.0")).toThrow(IncompatibleProtocolVersionError);
    expect(() => checkCompatible("2.0")).toThrow(
      "incompatible protocol_version 2.0: this side speaks 1.8; update the older side so both share MAJOR version 1",
    );
  });

  it("negotiates the lower MINOR and rejects malformed versions", () => {
    expect(negotiate("1.3", "1.1")).toBe("1.1");
    expect(negotiate("1.0", "1.2")).toBe("1.0");
    // 1.8 talks to every 1.x peer: the users bodies need nothing from a client of an older one.
    for (const peer of ["1.0", "1.3", "1.7", "1.8"]) expect(negotiate(peer)).toBe(peer);
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
    case "stt.status":
      return event.detail ?? event.state;
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

describe("REST error codes", () => {
  it("reads a known code and treats a missing or unknown one as none", () => {
    expect(errorCode({ detail: "Esa duda ya está cerrada.", code: "doubt_closed" })).toBe("doubt_closed");
    expect(errorCode({ detail: "x", code: "cost_cap_reached" })).toBe("cost_cap_reached");
    expect(errorCode({ detail: "x", code: "session_open" })).toBe("session_open");
    expect(errorCode({ detail: "x" })).toBeNull();
    expect(errorCode({ detail: "x", code: "added_in_1_9" })).toBeNull();
    expect(errorCode({ detail: "x", code: 409 })).toBeNull();
    expect(errorCode(undefined)).toBeNull();
    expect(errorCode("not an object")).toBeNull();
  });

  it("reads the two user codes of 1.8", () => {
    expect(isErrorCode("user_required")).toBe(true);
    expect(isErrorCode("added_in_1_9")).toBe(false);
    expect(errorCode({ detail: "¿Para quién es esto?", code: "user_required" })).toBe("user_required");
    expect(errorCode({ detail: "Aquí no hay nadie con ese nombre.", code: "user_not_found" })).toBe("user_not_found");
  });

  it("gives every code the version that added it", () => {
    expect(ERROR_CODE_SINCE).toEqual([1, 2]);
    expect(USER_ERROR_CODES_SINCE).toEqual([1, 8]);
    for (const code of ERROR_CODES) {
      const addedIn18 = code === "user_required" || code === "user_not_found";
      expect(ERROR_CODES_SINCE[code]).toEqual(addedIn18 ? USER_ERROR_CODES_SINCE : ERROR_CODE_SINCE);
    }
  });
});
