// Minimal runtime decoders for protocol messages: no dependency, strict like the JSON Schemas
// (unknown fields are refused, optional fields are absent rather than null).

/** A message does not match the v1 contract; `path` points at the offending field. */
export class ProtocolDecodeError extends Error {
  readonly path: string;

  constructor(path: string, message: string) {
    super(`${path || "<root>"}: ${message}`);
    this.name = "ProtocolDecodeError";
    this.path = path;
  }
}

export type Decoder<T> = (value: unknown, path: string) => T;

const at = (path: string, key: string | number): string =>
  typeof key === "number" ? `${path}[${key}]` : path ? `${path}.${key}` : key;

export function str(opts: { pattern?: RegExp; minLength?: number; maxLength?: number } = {}): Decoder<string> {
  return (value, path) => {
    if (typeof value !== "string") throw new ProtocolDecodeError(path, "expected a string");
    const length = [...value].length;
    if (opts.minLength !== undefined && length < opts.minLength) {
      throw new ProtocolDecodeError(path, `shorter than ${opts.minLength}`);
    }
    if (opts.maxLength !== undefined && length > opts.maxLength) {
      throw new ProtocolDecodeError(path, `longer than ${opts.maxLength}`);
    }
    if (opts.pattern && !opts.pattern.test(value)) {
      throw new ProtocolDecodeError(path, `does not match ${opts.pattern}`);
    }
    return value;
  };
}

export function int(opts: { min?: number; max?: number } = {}): Decoder<number> {
  return (value, path) => {
    if (typeof value !== "number" || !Number.isInteger(value)) {
      throw new ProtocolDecodeError(path, "expected an integer");
    }
    if (opts.min !== undefined && value < opts.min) throw new ProtocolDecodeError(path, `below ${opts.min}`);
    if (opts.max !== undefined && value > opts.max) throw new ProtocolDecodeError(path, `above ${opts.max}`);
    return value;
  };
}

export function num(opts: { min?: number; max?: number } = {}): Decoder<number> {
  return (value, path) => {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new ProtocolDecodeError(path, "expected a number");
    }
    if (opts.min !== undefined && value < opts.min) throw new ProtocolDecodeError(path, `below ${opts.min}`);
    if (opts.max !== undefined && value > opts.max) throw new ProtocolDecodeError(path, `above ${opts.max}`);
    return value;
  };
}

export function literal<const T extends string | number>(...allowed: T[]): Decoder<T> {
  return (value, path) => {
    if (!(allowed as unknown[]).includes(value)) {
      throw new ProtocolDecodeError(path, `expected one of ${allowed.map((a) => JSON.stringify(a)).join(", ")}`);
    }
    return value as T;
  };
}

export function array<T>(item: Decoder<T>, opts: { minItems?: number; maxItems?: number } = {}): Decoder<T[]> {
  return (value, path) => {
    if (!Array.isArray(value)) throw new ProtocolDecodeError(path, "expected an array");
    if (opts.minItems !== undefined && value.length < opts.minItems) {
      throw new ProtocolDecodeError(path, `fewer than ${opts.minItems} items`);
    }
    if (opts.maxItems !== undefined && value.length > opts.maxItems) {
      throw new ProtocolDecodeError(path, `more than ${opts.maxItems} items`);
    }
    return value.map((v, i) => item(v, at(path, i)));
  };
}

type Fields = Record<string, Decoder<unknown>>;
type Decoded<F extends Fields> = { [K in keyof F]: F[K] extends Decoder<infer T> ? T : never };
type Simplify<T> = { [K in keyof T]: T[K] } & {};

/**
 * A JSON object with `required` and `optional` fields and nothing else. The result is a fresh
 * object holding only the declared fields, with absent optionals left absent.
 */
export function object<R extends Fields, O extends Fields = Record<never, never>>(
  required: R,
  optional?: O,
): Decoder<Simplify<Decoded<R> & Partial<Decoded<O>>>> {
  return (value, path) => {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new ProtocolDecodeError(path, "expected an object");
    }
    const input = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(input)) {
      if (!(key in required) && !(optional && key in optional)) {
        throw new ProtocolDecodeError(at(path, key), "unknown field");
      }
    }
    for (const [key, decode] of Object.entries(required)) {
      if (!(key in input)) throw new ProtocolDecodeError(at(path, key), "missing required field");
      out[key] = decode(input[key], at(path, key));
    }
    for (const [key, decode] of Object.entries(optional ?? {})) {
      if (key in input) out[key] = decode(input[key], at(path, key));
    }
    return out as Simplify<Decoded<R> & Partial<Decoded<O>>>;
  };
}

/** Adds a cross-field rule (the schemas' `if`/`then`, `anyOf`) on top of a decoder. */
export function refine<T>(decode: Decoder<T>, rule: (value: T) => string | null): Decoder<T> {
  return (value, path) => {
    const decoded = decode(value, path);
    const problem = rule(decoded);
    if (problem !== null) throw new ProtocolDecodeError(path, problem);
    return decoded;
  };
}

/** A union discriminated on the wire field `type`; an unknown or missing `type` throws. */
export function discriminated<T extends { type: string }>(members: {
  [K in T["type"]]: Decoder<Extract<T, { type: K }>>;
}): Decoder<T> {
  return (value, path) => {
    const type = typeof value === "object" && value !== null ? (value as { type?: unknown }).type : undefined;
    if (typeof type !== "string" || !Object.hasOwn(members, type)) {
      throw new ProtocolDecodeError(at(path, "type"), `unknown or missing message type ${JSON.stringify(type)}`);
    }
    return (members as Record<string, Decoder<T>>)[type](value, path);
  };
}

// Field types shared by every message (base.py / protocol/README.md).
export const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const id = str({ pattern: ID_PATTERN });
export const epochMs = int({ min: 0 });
export const name = str({ minLength: 1, maxLength: 200 });
export const captureId = str({ pattern: UUID_PATTERN });
export const providerId = str({ minLength: 1, pattern: /^[a-z0-9]+(-[a-z0-9]+)*$/ });
export const language = str({ pattern: /^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$/ });
export const confidence = num({ min: 0, max: 1 });
