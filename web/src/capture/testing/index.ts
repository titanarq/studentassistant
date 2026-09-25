/**
 * The capture fakes (#40): everything a capture test needs in place of a camera, a microphone, the
 * Web Speech API, the session WebSocket and the Web Audio graph. Each installer returns the handles
 * a test drives and a `restore()`; `installCaptureFakes()` installs all of them at once.
 */

export * from "./audio";
export * from "./canvas";
export * from "./media";
export * from "./socket";
export * from "./speech";
export * from "./support";

import type { AudioFakes } from "./audio";
import { installAudioFakes } from "./audio";
import type { MediaFakeOptions, MediaFakes } from "./media";
import { installMediaFakes } from "./media";
import type { SocketFakes } from "./socket";
import { installWebSocketFake } from "./socket";
import type { SpeechFakes, SpeechGlobalName } from "./speech";
import { installSpeechRecognitionFake } from "./speech";

export interface CaptureFakeOptions {
  media?: MediaFakeOptions;
  /** Which globals offer the Web Speech API; both by default, none for a browser without it. */
  speechGlobals?: SpeechGlobalName[];
  /** True for a browser whose recognizer takes phrase hints (`SpeechRecognitionPhrase`). */
  speechPhrases?: boolean;
}

export interface CaptureFakes extends MediaFakes, SpeechFakes, SocketFakes, AudioFakes {}

/** Installs every capture fake and returns one `restore()` that takes all of them off again. */
export function installCaptureFakes(options: CaptureFakeOptions = {}): CaptureFakes {
  const media = installMediaFakes(options.media);
  const speech = installSpeechRecognitionFake(options.speechGlobals, {
    phrases: options.speechPhrases,
  });
  const sockets = installWebSocketFake();
  const audio = installAudioFakes();
  const installed = [media, speech, sockets, audio];
  return {
    ...media,
    ...speech,
    ...sockets,
    ...audio,
    restore: () => {
      for (const part of installed.reverse()) part.restore();
    },
  };
}
