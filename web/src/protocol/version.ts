// `protocol_version` (MAJOR.MINOR) and its negotiation, as defined in protocol/README.md.
// Peers with the same MAJOR interoperate and speak the lower of the two MINORs; a different
// MAJOR is refused with a message naming both versions.

export const PROTOCOL_VERSION = "1.3";

export const VERSION_PATTERN = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;

/** The peer speaks a protocol MAJOR version this side does not. */
export class IncompatibleProtocolVersionError extends Error {
  readonly peer: string;
  readonly ours: string;

  constructor(peer: string, ours: string = PROTOCOL_VERSION) {
    super(
      `incompatible protocol_version ${peer}: this side speaks ${ours}; ` +
        `update the older side so both share MAJOR version ${parseVersion(ours)[0]}`,
    );
    this.name = "IncompatibleProtocolVersionError";
    this.peer = peer;
    this.ours = ours;
  }
}

/** Splits `MAJOR.MINOR` into integers; anything else throws. */
export function parseVersion(version: string): [major: number, minor: number] {
  const match = VERSION_PATTERN.exec(version);
  if (match === null) {
    throw new Error(`malformed protocol_version ${JSON.stringify(version)}: expected MAJOR.MINOR`);
  }
  return [Number(match[1]), Number(match[2])];
}

/** Throws `IncompatibleProtocolVersionError` unless `peer` shares our MAJOR version. */
export function checkCompatible(peer: string, ours: string = PROTOCOL_VERSION): void {
  if (parseVersion(peer)[0] !== parseVersion(ours)[0]) {
    throw new IncompatibleProtocolVersionError(peer, ours);
  }
}

/** The version both sides speak: the shared MAJOR with the lower MINOR. */
export function negotiate(peer: string, ours: string = PROTOCOL_VERSION): string {
  checkCompatible(peer, ours);
  return parseVersion(peer)[1] <= parseVersion(ours)[1] ? peer : ours;
}
