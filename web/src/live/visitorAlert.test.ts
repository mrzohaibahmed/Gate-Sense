import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UnknownMessage } from "./useLiveEvents";

const visit = { type: "unknown", id: 7, camera: "gate", closest_name: "Ayesha", score: 0.31 } as UnknownMessage;

/** A fake Web Audio context: when each beep starts, and the early stops (stop() with no time). */
class FakeAudio {
  static started = 0;
  static starts: number[] = [];
  static stoppedEarly = 0;
  static instance: FakeAudio | null = null;
  constructor() {
    FakeAudio.instance = this;
  }
  state = "running";
  currentTime = 0;
  destination = {};
  resume = vi.fn(() => Promise.resolve());
  createOscillator() {
    return {
      type: "",
      frequency: { value: 0 },
      connect: (n: unknown) => n,
      start: (t: number) => {
        FakeAudio.started++;
        FakeAudio.starts.push(t);
      },
      stop: (t?: number) => {
        if (t === undefined) FakeAudio.stoppedEarly++;
      },
    };
  }
  createGain() {
    const node = {
      gain: { setValueAtTime: () => {}, exponentialRampToValueAtTime: () => {} },
      connect: (n: unknown) => n,
    };
    return node;
  }
}

class FakeNotification {
  static permission: NotificationPermission = "granted";
  static shown: { title: string; options?: NotificationOptions }[] = [];
  static last: FakeNotification | null = null;
  static requestPermission = vi.fn(() => Promise.resolve(FakeNotification.permission));
  onclick: (() => void) | null = null;
  close = vi.fn();
  constructor(title: string, options?: NotificationOptions) {
    FakeNotification.shown.push({ title, options });
    FakeNotification.last = this;
  }
}

async function load() {
  vi.resetModules(); // the module reads its settings and makes its audio context once
  return import("./visitorAlert");
}

beforeEach(() => {
  window.localStorage.clear();
  FakeAudio.started = 0;
  FakeAudio.starts = [];
  FakeAudio.stoppedEarly = 0;
  FakeNotification.permission = "granted";
  FakeNotification.shown = [];
  FakeNotification.last = null;
  vi.stubGlobal("AudioContext", FakeAudio);
  vi.stubGlobal("Notification", FakeNotification);
  vi.stubGlobal("isSecureContext", true);
  vi.spyOn(window, "focus").mockImplementation(() => {}); // jsdom has none
});

afterEach(() => vi.unstubAllGlobals());

describe("visitor alert", () => {
  it("beeps and shows a desktop notification that opens the visitor list", async () => {
    const m = await load();
    const open = vi.fn();
    m.alertVisitor(visit, open);
    expect(FakeAudio.started).toBe(20); // a beep-beep every 0.5 s ...
    const last = Math.max(...FakeAudio.starts);
    expect(last).toBeGreaterThan(4.5); // ... for 5 s
    expect(last).toBeLessThan(5);
    expect(FakeNotification.shown).toEqual([
      {
        title: "Unknown visitor at gate",
        options: expect.objectContaining({ body: "Matches nobody enrolled (closest: Ayesha, 0.31)", tag: "visitor-7" }),
      },
    ]);
    FakeNotification.last!.onclick!();
    expect(open).toHaveBeenCalled();
  });

  it("stays quiet when switched off, and remembers it", async () => {
    let m = await load();
    m.setAlertPrefs({ sound: false, desktop: false });
    m = await load();
    expect(m.alertPrefs()).toEqual({ sound: false, desktop: false });
    m.alertVisitor(visit, () => {});
    expect(FakeAudio.started).toBe(0);
    expect(FakeNotification.shown).toEqual([]);
  });

  it("shows no desktop notification without the browser's permission or a secure page", async () => {
    FakeNotification.permission = "default";
    let m = await load();
    expect(m.desktopState()).toBe("default");
    m.alertVisitor(visit, () => {});
    expect(FakeNotification.shown).toEqual([]);
    expect(FakeAudio.started).toBe(20); // the beep still sounds

    vi.stubGlobal("isSecureContext", false); // plain http from another PC
    m = await load();
    expect(m.desktopState()).toBe("unsupported");
    expect(await m.requestDesktop()).toBe("unsupported");
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
  });

  it("stops early once seen, and a new alert replaces one still sounding", async () => {
    const m = await load();
    m.beep();
    m.stopBeep();
    expect(FakeAudio.stoppedEarly).toBe(20);
    m.stopBeep(); // nothing left
    expect(FakeAudio.stoppedEarly).toBe(20);
    m.beep();
    m.beep(1); // the preview: 1 s
    expect(FakeAudio.stoppedEarly).toBe(40);
    expect(FakeAudio.started).toBe(20 + 20 + 4);
  });

  it("works in a browser without Web Audio", async () => {
    vi.stubGlobal("AudioContext", undefined);
    const m = await load();
    expect(() => m.beep()).not.toThrow();
  });

  it("unlocks the audio on the first click", async () => {
    const m = await load();
    const remove = m.installAudioUnlock();
    window.dispatchEvent(new Event("pointerdown"));
    const ctx = FakeAudio.instance!;
    ctx.state = "suspended";
    window.dispatchEvent(new Event("keydown"));
    expect(ctx.resume).toHaveBeenCalledTimes(1);
    remove();
    window.dispatchEvent(new Event("pointerdown"));
    expect(ctx.resume).toHaveBeenCalledTimes(1);
  });
});
