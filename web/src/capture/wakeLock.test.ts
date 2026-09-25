/**
 * The screen wake lock of a running session (#256), against the fake `navigator.wakeLock` and a
 * fake page visibility: acquired on start, asked for again when the page comes back into view,
 * given back on stop, and silent in a browser that has no API or refuses.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { type FakeWakeLock, installWakeLockFake, setVisibility, swapProperty } from "./testing";
import { ScreenWakeLock } from "./wakeLock";

let wakeLock: FakeWakeLock;
const restores: Array<() => void> = [];

/** Lets the promise chain of a request settle. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
}

beforeEach(() => {
  const fake = installWakeLockFake();
  wakeLock = fake.wakeLock;
  restores.push(fake.restore);
  restores.push(swapProperty(document, "visibilityState", "visible"));
});

afterEach(() => {
  for (const restore of restores.splice(0).reverse()) restore();
});

describe("ScreenWakeLock", () => {
  it("holds the screen awake from start", async () => {
    const lock = new ScreenWakeLock();
    lock.start();
    await settle();

    expect(wakeLock.requestCount).toBe(1);
    expect(wakeLock.held()).toHaveLength(1);
    expect(lock.held).toBe(true);
    lock.stop();
  });

  it("asks again when the page becomes visible after the browser let it go", async () => {
    const lock = new ScreenWakeLock();
    lock.start();
    await settle();

    // A hidden tab: the browser releases the lock by itself and nothing is asked while hidden.
    restores.push(setVisibility("hidden"));
    wakeLock.sentinels[0].releaseFromBrowser();
    await settle();
    expect(lock.held).toBe(false);
    expect(wakeLock.requestCount).toBe(1);

    restores.push(setVisibility("visible"));
    await settle();

    expect(wakeLock.requestCount).toBe(2);
    expect(wakeLock.held()).toEqual([wakeLock.sentinels[1]]);
    lock.stop();
  });

  it("does not ask twice while the lock is still held", async () => {
    const lock = new ScreenWakeLock();
    lock.start();
    await settle();

    restores.push(setVisibility("visible"));
    await settle();

    expect(wakeLock.requestCount).toBe(1);
    lock.stop();
  });

  it("gives the lock back on stop and stops listening", async () => {
    const lock = new ScreenWakeLock();
    lock.start();
    await settle();

    lock.stop();
    lock.stop();
    await settle();

    expect(wakeLock.sentinels[0].releaseCount).toBe(1);
    expect(wakeLock.held()).toHaveLength(0);
    restores.push(setVisibility("hidden"));
    restores.push(setVisibility("visible"));
    await settle();
    expect(wakeLock.requestCount).toBe(1);
  });

  it("lets go of a lock that arrives after the session stopped", async () => {
    const lock = new ScreenWakeLock();
    lock.start();
    lock.stop();
    await settle();

    expect(wakeLock.requestCount).toBe(1);
    expect(wakeLock.held()).toHaveLength(0);
  });

  it("is silent in a browser without the API", async () => {
    const lock = new ScreenWakeLock({ wakeLock: null });
    expect(() => lock.start()).not.toThrow();
    await settle();
    expect(lock.held).toBe(false);
    expect(() => lock.stop()).not.toThrow();
  });

  it("finds no API when navigator has none", async () => {
    restores.push(swapProperty(navigator, "wakeLock", undefined));
    const lock = new ScreenWakeLock();
    lock.start();
    await settle();
    expect(lock.held).toBe(false);
    lock.stop();
  });

  it("is silent when the browser refuses the lock", async () => {
    wakeLock.failure = "NotAllowedError";
    const lock = new ScreenWakeLock();
    lock.start();
    await settle();

    expect(wakeLock.requestCount).toBe(1);
    expect(lock.held).toBe(false);
    lock.stop();
  });
});
