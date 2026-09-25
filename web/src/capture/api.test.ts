// @vitest-environment node
// The answers under test are the repository's own `protocol/examples/`, and the helper that reads
// them needs `import.meta.url` to stay a file URL, as in `protocol.test.ts`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CaptureUploadRequest,
  CaptureUploadResponse,
  Session,
  TopicsListResponse,
} from "../protocol";
import { sharedExamples } from "../test/protocolExamples";
import {
  type CapturedImage,
  createSubject,
  createTopic,
  endSession,
  listSubjects,
  listTopics,
  METADATA_PART,
  resumeSession,
  SESSIONS_PATH,
  sessionCapturesPath,
  sessionEndPath,
  sessionResumePath,
  startSession,
  SUBJECTS_PATH,
  topicsPath,
  uploadCaptures,
} from "./api";

const SUBJECT_ID = "biologia";
const TOPIC_ID = "fotosintesis";
const SESSION_ID = "s-20260924-1810";
const CLIENT_TIME_MS = 1790251200000;

const EXAMPLES = Object.fromEntries(sharedExamples().map((entry) => [entry.name, entry.json]));

/** One of the repository's own `protocol/examples/` messages, so the answers are contract-valid. */
function example<T = unknown>(name: string): T {
  const json = EXAMPLES[name];
  if (json === undefined) throw new Error(`there is no shared example named ${name}`);
  return json as T;
}

interface Sent {
  readonly path: string;
  readonly init: RequestInit;
}

const sent: Sent[] = [];

function stubFetch(answer: Response | (() => Response)): void {
  const respond = typeof answer === "function" ? answer : () => answer;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      sent.push({ path, init });
      return respond();
    }),
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function jsonBody(call: Sent): unknown {
  return JSON.parse(call.init.body as string);
}

function formOf(call: Sent): FormData {
  return call.init.body as FormData;
}

function partBlob(form: FormData, name: string): Blob {
  const part = form.get(name);
  if (part === null || typeof part === "string") throw new Error(`no blob part named ${name}`);
  return part;
}

/** The burst of the shared example, with bytes that are deliberately not the declared type. */
function burst(): { metadata: CaptureUploadRequest; images: CapturedImage[] } {
  const metadata = example<CaptureUploadRequest>("rest.sessions.captures.request");
  return {
    metadata,
    images: metadata.images.map((image, index) => ({
      part: image.part,
      blob: new Blob([`still-${index}`], { type: "image/png" }),
    })),
  };
}

beforeEach(() => {
  sent.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("subjects", () => {
  it("lists them with a bare GET of /api/subjects", async () => {
    stubFetch(jsonResponse(example("rest.subjects.list.response")));

    expect(await listSubjects()).toEqual({
      kind: "ok",
      value: example("rest.subjects.list.response"),
    });
    expect(sent).toEqual([{ path: SUBJECTS_PATH, init: { method: "GET" } }]);
  });

  it("creates one with its name as the whole JSON body", async () => {
    stubFetch(jsonResponse(example("rest.subjects.create.response"), 201));

    expect(await createSubject("Biología")).toEqual({
      kind: "ok",
      value: example("rest.subjects.create.response"),
    });
    expect(sent).toEqual([
      {
        path: SUBJECTS_PATH,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: "Biología" }),
        },
      },
    ]);
    expect(jsonBody(sent[0])).toEqual({ name: "Biología" });
  });
});

describe("topics", () => {
  it("lists a subject's topics, keeping the 1.1 and 1.3 fields and the ones a topic does not have", async () => {
    stubFetch(jsonResponse(example("rest.topics.list.response")));

    const result = await listTopics(SUBJECT_ID);
    expect(result).toEqual({ kind: "ok", value: example("rest.topics.list.response") });
    expect(sent).toEqual([{ path: topicsPath(SUBJECT_ID), init: { method: "GET" } }]);
    if (result.kind !== "ok") return;
    expect(result.value.topics[0]).toEqual({
      topic_id: "la-celula",
      subject_id: SUBJECT_ID,
      name: "La célula",
      open_session_id: "s-20260924-1805",
      last_session_at_ms: 1790273100000,
      pending_count: 2,
      digest_excerpt: example<TopicsListResponse>("rest.topics.list.response").topics[0].digest_excerpt,
    });
    expect(result.value.topics[1]).not.toHaveProperty("open_session_id");
  });

  it("creates one under its subject with its name as the whole JSON body", async () => {
    stubFetch(jsonResponse(example("rest.topics.create.response"), 201));

    expect(await createTopic(SUBJECT_ID, "Fotosíntesis")).toEqual({
      kind: "ok",
      value: example("rest.topics.create.response"),
    });
    expect(sent[0].path).toBe(`/api/subjects/${SUBJECT_ID}/topics`);
    expect(jsonBody(sent[0])).toEqual({ name: "Fotosíntesis" });
  });

  it("quotes the subject id it puts in a path", async () => {
    stubFetch(jsonResponse({ subject_id: "bio-logia", topics: [] }));

    await listTopics("bio-logia/../../otro");

    expect(sent[0].path).toBe("/api/subjects/bio-logia%2F..%2F..%2Fotro/topics");
  });
});

describe("session lifecycle", () => {
  it("starts a session naming the subject, the topic and the client's clock", async () => {
    stubFetch(jsonResponse(example("rest.sessions.start.response"), 201));

    expect(await startSession(SUBJECT_ID, TOPIC_ID, CLIENT_TIME_MS)).toEqual({
      kind: "ok",
      value: example("rest.sessions.start.response"),
    });
    expect(sent).toEqual([
      {
        path: SESSIONS_PATH,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(example("rest.sessions.start.request")),
        },
      },
    ]);
    expect(jsonBody(sent[0])).toEqual({
      subject_id: SUBJECT_ID,
      topic_id: TOPIC_ID,
      client_time_ms: CLIENT_TIME_MS,
    });
  });

  it("keeps the ws_path and the captures the backend already stored", async () => {
    stubFetch(jsonResponse(example("rest.sessions.resume.response")));

    const result = await resumeSession("s-20260924-1805");

    expect(sent).toEqual([
      { path: sessionResumePath("s-20260924-1805"), init: { method: "POST" } },
    ]);
    expect(result).toEqual({
      kind: "ok",
      value: expect.objectContaining({
        ws_path: "/ws/sessions/s-20260924-1805",
        received_capture_ids: ["0b6f5d0e-3c1a-4a8e-9d2b-7f4e1c9a5b30"],
      }),
    });
  });

  it("ends a session with the reason and the client's clock", async () => {
    stubFetch(jsonResponse(example("rest.sessions.end.response")));

    expect(await endSession(SESSION_ID, "button", CLIENT_TIME_MS)).toEqual({
      kind: "ok",
      value: example("rest.sessions.end.response"),
    });
    expect(sent[0].path).toBe(sessionEndPath(SESSION_ID));
    expect(jsonBody(sent[0])).toEqual({ client_time_ms: CLIENT_TIME_MS, reason: "button" });
  });

  it("ends a session the backend asked to end with reason command", async () => {
    stubFetch(jsonResponse(example("rest.sessions.end.response")));

    await endSession(SESSION_ID, "command", CLIENT_TIME_MS);

    expect(jsonBody(sent[0])).toEqual({ client_time_ms: CLIENT_TIME_MS, reason: "command" });
  });
});

describe("capture upload", () => {
  it("sends the metadata part and one part per image, each as its metadata declares", async () => {
    stubFetch(jsonResponse(example("rest.sessions.captures.response"), 201));
    const { metadata, images } = burst();

    expect(await uploadCaptures(SESSION_ID, metadata, images)).toEqual({
      kind: "ok",
      value: example("rest.sessions.captures.response"),
    });

    expect(sent).toHaveLength(1);
    expect(sent[0].path).toBe(sessionCapturesPath(SESSION_ID));
    expect(sent[0].init.method).toBe("POST");
    // The browser sets the multipart Content-Type with its own boundary; asking for one breaks it.
    expect(sent[0].init.headers).toBeUndefined();
    const form = formOf(sent[0]);
    expect(Array.from(form.keys())).toEqual([METADATA_PART, "image_0", "image_1", "image_2"]);
    expect(partBlob(form, METADATA_PART).type).toBe("application/json");
    expect(JSON.parse(await partBlob(form, METADATA_PART).text())).toEqual(metadata);
    for (const [index, image] of metadata.images.entries()) {
      const part = partBlob(form, image.part);
      // The blob was made as image/png: what goes on the wire is what the metadata declares.
      expect(part.type).toBe(image.content_type);
      expect(await part.text()).toBe(`still-${index}`);
    }
  });

  it("leaves an absent command_id out of the metadata part instead of sending it as null", async () => {
    stubFetch(jsonResponse(example("rest.sessions.captures.response"), 201));
    const burstOfThree = burst();
    const metadata: CaptureUploadRequest = {
      ...burstOfThree.metadata,
      trigger: "button",
      command_id: undefined,
      images: burstOfThree.metadata.images.slice(0, 1),
    };

    await uploadCaptures(SESSION_ID, metadata, burstOfThree.images.slice(0, 1));

    const form = formOf(sent[0]);
    expect(Array.from(form.keys())).toEqual([METADATA_PART, "image_0"]);
    const sentMetadata = JSON.parse(await partBlob(form, METADATA_PART).text()) as Record<
      string,
      unknown
    >;
    expect("command_id" in sentMetadata).toBe(false);
    expect(sentMetadata).toEqual(metadata);
    expect(sentMetadata.trigger).toBe("button");
  });

  it("takes a repeated capture_id as stored, which is what the backend answers", async () => {
    const duplicate: CaptureUploadResponse = {
      ...example<CaptureUploadResponse>("rest.sessions.captures.response"),
      status: "duplicate",
    };
    stubFetch(jsonResponse(duplicate));
    const { metadata, images } = burst();

    expect(await uploadCaptures(SESSION_ID, metadata, images)).toEqual({
      kind: "ok",
      value: duplicate,
    });
  });

  it("passes a burst the backend will not take through as a refusal, with its Spanish detail", async () => {
    stubFetch(
      jsonResponse({ detail: "Falta la imagen «image_1» que declara «metadata»." }, 422),
    );
    const { metadata, images } = burst();

    expect(await uploadCaptures(SESSION_ID, metadata, images)).toEqual({
      kind: "refused",
      status: 422,
      detail: "Falta la imagen «image_1» que declara «metadata».",
    });
  });
});

describe("an answer that is not the promised message", () => {
  it("reports the field the decoder missed instead of using the body", async () => {
    stubFetch(jsonResponse({ subjects: [{ subject_id: SUBJECT_ID }] }));

    expect(await listSubjects()).toEqual({
      kind: "unexpected",
      status: 200,
      expected: "rest.subjects.list.response",
      problem: "subjects[0].name: missing required field",
    });
  });

  it("reports a field the protocol does not have", async () => {
    stubFetch(jsonResponse({ subjects: [], subject: SUBJECT_ID }));

    expect(await listSubjects()).toEqual({
      kind: "unexpected",
      status: 200,
      expected: "rest.subjects.list.response",
      problem: "subject: unknown field",
    });
  });

  it("reports a session whose ws_path is not the protocol's", async () => {
    stubFetch(
      jsonResponse(
        { ...example<Session>("rest.sessions.start.response"), ws_path: "ws/sessions" },
        201,
      ),
    );

    expect(await startSession(SUBJECT_ID, TOPIC_ID, CLIENT_TIME_MS)).toEqual({
      kind: "unexpected",
      status: 201,
      expected: "rest.sessions.start.response",
      problem: expect.stringContaining("ws_path"),
    });
  });

  it("reports a 2xx body that is not JSON at all", async () => {
    stubFetch(new Response("<!doctype html><title>Backend</title>", { status: 200 }));

    expect(await listTopics(SUBJECT_ID)).toEqual({
      kind: "unexpected",
      status: 200,
      expected: "rest.topics.list.response",
      problem: "<root>: expected an object",
    });
  });
});

describe("a request that did not get its answer", () => {
  it("passes the backend's Spanish refusal through as it comes", async () => {
    stubFetch(jsonResponse({ detail: "Ya hay una sesión abierta en este tema." }, 409));

    expect(await startSession(SUBJECT_ID, TOPIC_ID, CLIENT_TIME_MS)).toEqual({
      kind: "refused",
      status: 409,
      detail: "Ya hay una sesión abierta en este tema.",
    });
  });

  it("maps a refusal of an unknown session, with its status", async () => {
    stubFetch(jsonResponse({ detail: "La sesión s-1 no existe." }, 404));

    expect(await resumeSession("s-1")).toEqual({
      kind: "refused",
      status: 404,
      detail: "La sesión s-1 no existe.",
    });
  });

  it("maps a non-2xx answer whose detail is not text to an error with its status", async () => {
    stubFetch(jsonResponse({ detail: { msg: "boom" } }, 500));

    expect(await listSubjects()).toEqual({ kind: "error", status: 500 });
  });

  it("maps a non-2xx answer without a JSON body to an error with its status", async () => {
    stubFetch(new Response("El almacén no está disponible.", { status: 503 }));

    expect(await endSession(SESSION_ID, "button", CLIENT_TIME_MS)).toEqual({
      kind: "error",
      status: 503,
    });
  });

  it("maps a request that never got an answer to unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    expect(await uploadCaptures(SESSION_ID, burst().metadata, burst().images)).toEqual({
      kind: "unreachable",
    });
    expect(sent).toEqual([]);
  });
});
