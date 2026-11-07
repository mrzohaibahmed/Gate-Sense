import { useSyncExternalStore } from "react";

import type { UnknownMessage } from "./useLiveEvents";

/**
 * The alert for a new unknown visitor, on top of the toast: a beep and a desktop notification (the browser's
 * Notification, seen even when the dashboard is behind another window). Both are this browser's choice (the guard's
 * PC beeps, an admin's laptop needn't), kept in localStorage: sound on by default, desktop alerts once the browser
 * allows them. Browsers play sound only after the page has had a click or key press, so the first one unlocks it.
 */
export type AlertPrefs = { sound: boolean; desktop: boolean };

const KEY = "gatesense.visitorAlert";
const DEFAULTS: AlertPrefs = { sound: true, desktop: true };

let prefs: AlertPrefs = readPrefs();
const listeners = new Set<() => void>();

function readPrefs(): AlertPrefs {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const v = JSON.parse(raw) as Partial<AlertPrefs>;
    return { sound: v.sound !== false, desktop: v.desktop !== false };
  } catch {
    return { ...DEFAULTS };
  }
}

export function alertPrefs(): AlertPrefs {
  return prefs;
}

export function setAlertPrefs(change: Partial<AlertPrefs>): void {
  prefs = { ...prefs, ...change };
  try {
    window.localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    /* not remembered */
  }
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useAlertPrefs(): AlertPrefs {
  return useSyncExternalStore(subscribe, alertPrefs, alertPrefs);
}

// --- Sound -------------------------------------------------------------------------------------------------------

type AudioCtor = typeof AudioContext;
let audio: AudioContext | null = null;

function audioContext(): AudioContext | null {
  if (audio) return audio;
  const Ctor: AudioCtor | undefined =
    window.AudioContext ?? (window as unknown as { webkitAudioContext?: AudioCtor }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    audio = new Ctor();
  } catch {
    return null;
  }
  return audio;
}

/** Let the page make sound: browsers keep audio suspended until a click or key press. */
export function installAudioUnlock(): () => void {
  const unlock = () => {
    const ctx = audioContext();
    if (ctx && ctx.state === "suspended") void ctx.resume().catch(() => undefined);
  };
  window.addEventListener("pointerdown", unlock, true);
  window.addEventListener("keydown", unlock, true);
  return () => {
    window.removeEventListener("pointerdown", unlock, true);
    window.removeEventListener("keydown", unlock, true);
  };
}

/** How long the alert sounds (s), unless someone clicks the toast or the notification first. */
export const BEEP_SECONDS = 5;
/** One beep-beep (a high and a higher tone) every this long (s). */
const BEEP_CYCLE = 0.5;

let playing: OscillatorNode[] = [];

/** Beep-beep, over and over, for `seconds` (BEEP_SECONDS). A new alert replaces one still sounding. */
export function beep(seconds: number = BEEP_SECONDS): void {
  const ctx = audioContext();
  if (!ctx) return;
  stopBeep();
  try {
    if (ctx.state === "suspended") void ctx.resume().catch(() => undefined);
    const start = ctx.currentTime + 0.02;
    const cycles = Math.max(1, Math.round(seconds / BEEP_CYCLE));
    for (let c = 0; c < cycles; c++) {
      for (const [offset, hz] of [
        [0, 990],
        [0.2, 1320],
      ] as const) {
        const t = start + c * BEEP_CYCLE + offset;
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "square";
        osc.frequency.value = hz;
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.25, t + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.15);
        osc.connect(gain).connect(ctx.destination);
        osc.start(t);
        osc.stop(t + 0.16);
        playing.push(osc);
      }
    }
  } catch {
    /* no sound on this browser */
  }
}

/** Silence the alert now (the toast's View or a click on the notification: someone has seen it). */
export function stopBeep(): void {
  for (const osc of playing) {
    try {
      osc.stop();
    } catch {
      /* already stopped */
    }
  }
  playing = [];
}

// --- Desktop notification ----------------------------------------------------------------------------------------

/**
 * "unsupported": no Notification API, or the page isn't a secure context (plain http from another PC; localhost and
 * https are fine). Otherwise the browser's permission.
 */
export type DesktopState = "unsupported" | NotificationPermission;

export function desktopState(): DesktopState {
  if (typeof window.Notification === "undefined" || window.isSecureContext === false) return "unsupported";
  return window.Notification.permission;
}

/** Ask the browser (only from a click: browsers ignore a request without one). */
export async function requestDesktop(): Promise<DesktopState> {
  if (desktopState() === "unsupported") return "unsupported";
  try {
    return await window.Notification.requestPermission();
  } catch {
    return desktopState();
  }
}

export function visitorText(v: UnknownMessage): { title: string; body: string } {
  return {
    title: `Unknown visitor at ${v.camera}`,
    body: v.closest_name
      ? `Matches nobody enrolled (closest: ${v.closest_name}, ${v.score.toFixed(2)})`
      : "Matches nobody enrolled",
  };
}

function notifyDesktop(v: UnknownMessage, open: () => void): void {
  if (desktopState() !== "granted") return;
  const { title, body } = visitorText(v);
  try {
    const n = new window.Notification(title, { body, tag: `visitor-${v.id}`, requireInteraction: false });
    n.onclick = () => {
      window.focus();
      open();
      n.close();
    };
  } catch {
    /* some browsers allow notifications only from a service worker */
  }
}

/** Beep and show the desktop notification for a new visit, as this browser's settings say. */
export function alertVisitor(v: UnknownMessage, open: () => void): void {
  if (prefs.sound) beep();
  if (prefs.desktop) notifyDesktop(v, open);
}
