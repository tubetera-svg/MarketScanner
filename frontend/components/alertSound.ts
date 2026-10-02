// Browser-side alert tones, shared by the scanner page and the global
// price-alert notifier (components/PriceAlertNotifier.tsx). Which sound each
// alert plays, volume and quiet hours come from the "sounds" block of
// /api/settings (api/app_settings.py); a temporary mute is per browser.

import { fetchAppSettings } from "./appSettings";

export type AlertSoundKind = "ltf" | "silver_bullet" | "price_alert" | "news_event";
type SoundChoice = { enabled: boolean; sound: string };
export type SoundSettings = {
  enabled: boolean;
  volume: number;
  ltf: SoundChoice;
  silver_bullet: SoundChoice;
  price_alert: SoundChoice;
  news_event: SoundChoice & { lead_minutes: number; repeat: number };
  quiet_hours: { enabled: boolean; start: string; end: string };
};

// Mirrors DEFAULTS["sounds"] in api/app_settings.py; used until the API answers.
const DEFAULT_SETTINGS: SoundSettings = {
  enabled: true,
  volume: 70,
  ltf: { enabled: true, sound: "chime" },
  silver_bullet: { enabled: true, sound: "ping" },
  price_alert: { enabled: true, sound: "doorbell" },
  news_event: { enabled: false, sound: "bell", lead_minutes: 5, repeat: 3 },
  quiet_hours: { enabled: false, start: "23:00", end: "07:00" },
};

// at/dur in seconds; `to` = pitch slide end; gain relative to the master volume.
type Note = { f: number; at: number; dur: number; type?: OscillatorType; to?: number; gain?: number };

// Keep ids in step with SOUND_CHOICES in api/app_settings.py.
const SOUNDS: Record<string, { label: string; notes: Note[] }> = {
  chime: { label: "Chime", notes: [{ f: 1047, at: 0, dur: 0.18 }, { f: 1319, at: 0.14, dur: 0.18 }, { f: 1568, at: 0.28, dur: 0.35 }] },
  ping: {
    label: "Double ping",
    notes: [{ f: 1760, at: 0, dur: 0.09, type: "triangle" }, { f: 1760, at: 0.14, dur: 0.09, type: "triangle" }, { f: 1175, at: 0.3, dur: 0.4, type: "triangle" }],
  },
  doorbell: { label: "Doorbell", notes: [{ f: 784, at: 0, dur: 0.4 }, { f: 1047, at: 0.55, dur: 0.4 }] },
  beeps: { label: "Urgent beeps", notes: [988, 1319, 988, 1319].map((f, i) => ({ f, at: i * 0.23, dur: 0.16 })) },
  rising: { label: "Rising", notes: [{ f: 440, to: 1320, at: 0, dur: 0.6 }] },
  falling: { label: "Falling", notes: [{ f: 1320, to: 440, at: 0, dur: 0.6 }] },
  bell: { label: "Bell", notes: [{ f: 880, at: 0, dur: 1.2 }, { f: 1760, at: 0, dur: 1.0, gain: 0.4 }, { f: 2640, at: 0, dur: 0.8, gain: 0.2 }] },
  marimba: {
    label: "Marimba",
    notes: [{ f: 523, at: 0, dur: 0.15, type: "triangle" }, { f: 659, at: 0.12, dur: 0.15, type: "triangle" }, { f: 784, at: 0.24, dur: 0.25, type: "triangle" }],
  },
  siren: { label: "Siren", notes: [700, 950, 700, 950, 700, 950].map((f, i) => ({ f, at: i * 0.2, dur: 0.2, type: "square" as const, gain: 0.4 })) },
  tick: { label: "Soft tick", notes: [{ f: 2000, at: 0, dur: 0.03, type: "square", gain: 0.4 }] },
  // Longer (2–3 s) sounds, meant for news / scheduled events.
  pulse_alarm: {
    label: "Pulse alarm (long)",
    notes: [0, 0.6, 1.2, 1.8].flatMap((at) => [at, at + 0.15].map((t) => ({ f: 1047, at: t, dur: 0.1, type: "square" as const, gain: 0.8 }))).concat([{ f: 1047, at: 2.4, dur: 0.4, type: "square", gain: 0.8 }]),
  },
  harp: {
    label: "Harp sweep (long)",
    notes: [523, 659, 784, 1047, 1319, 1568, 2093].map((f, i) => ({ f, at: i * 0.12, dur: 0.35, type: "triangle" as const, gain: 1.2 }))
      .concat([523, 659, 784].map((f) => ({ f, at: 0.9, dur: 1.6, type: "triangle" as const, gain: 0.8 }))),
  },
  sparkle: {
    label: "Sparkle (long)",
    // High run plus two fading echoes.
    notes: [0, 0.45, 0.9].flatMap((at, echo) => [1568, 1976, 2349, 3136].map((f, i) => ({ f, at: at + i * 0.08, dur: 0.6, gain: 0.9 / (echo + 1) })))
      .concat([{ f: 2093, at: 1.4, dur: 1.2, gain: 0.7 }, { f: 3136, at: 1.4, dur: 1.0, gain: 0.3 }]),
  },
  notify_melody: {
    label: "Notify melody (long)",
    notes: [1319, 988, 1175, 1568, 1319].flatMap((f, i) => [
      { f, at: i * 0.32, dur: i === 4 ? 1.2 : 0.45, gain: 1.1 },
      { f: f * 2, at: i * 0.32, dur: i === 4 ? 0.8 : 0.3, gain: 0.25 },
    ]),
  },
  vibes: {
    label: "Vibraphone chord (long)",
    // Slightly detuned pairs give a shimmering vibrato.
    notes: [784, 988, 1175, 1480].flatMap((f, i) => [
      { f, at: i * 0.15, dur: 2.2, gain: 0.8 },
      { f: f * 1.006, at: i * 0.15, dur: 2.2, gain: 0.5 },
    ]),
  },
  fanfare: {
    label: "News fanfare (long)",
    notes: [
      { f: 784, at: 0, dur: 0.15, type: "sawtooth", gain: 0.35 },
      { f: 784, at: 0.2, dur: 0.15, type: "sawtooth", gain: 0.35 },
      { f: 784, at: 0.4, dur: 0.15, type: "sawtooth", gain: 0.35 },
      { f: 1047, at: 0.6, dur: 0.7, type: "sawtooth", gain: 0.35 },
      { f: 1319, at: 1.35, dur: 1.2, type: "sawtooth", gain: 0.3 },
      { f: 1568, at: 1.35, dur: 1.2, gain: 0.6 },
    ],
  },
};

export const soundLabel = (id: string) => SOUNDS[id]?.label ?? id;

type AudioContextWindow = Window & { webkitAudioContext?: typeof AudioContext };
let audioContext: AudioContext | null = null;

const getContext = (): AudioContext | null => {
  if (typeof window === "undefined") return null;
  const Context = window.AudioContext ?? (window as AudioContextWindow).webkitAudioContext;
  if (!Context) return null;
  audioContext = audioContext ?? new Context();
  if (audioContext.state === "suspended") void audioContext.resume().catch(() => {});
  return audioContext;
};

const playNotes = (notes: Note[], volume: number) => {
  const peakBase = 0.3 * Math.max(0, Math.min(100, volume)) / 100;
  const context = getContext();
  if (!context || peakBase <= 0) return;
  const startAt = context.currentTime + 0.05;
  for (const note of notes) {
    const start = startAt + note.at;
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = note.type ?? "sine";
    oscillator.frequency.setValueAtTime(note.f, start);
    if (note.to) oscillator.frequency.exponentialRampToValueAtTime(note.to, start + note.dur);
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(Math.max(0.0002, peakBase * (note.gain ?? 1)), start + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + note.dur);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(start);
    oscillator.stop(start + note.dur);
  }
};

// --- settings cache -------------------------------------------------------
let settings: SoundSettings = DEFAULT_SETTINGS;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

export const getSoundSettings = () => settings;
export const setSoundSettings = (next: SoundSettings | undefined) => {
  if (!next) return;
  settings = next;
  notify();
};
export const subscribeSoundSettings = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

const refreshSoundSettings = () => {
  fetchAppSettings<{ settings?: { sounds?: SoundSettings } }>()
    .then((data) => setSoundSettings(data.settings?.sounds))
    .catch(() => {});
};

if (typeof window !== "undefined") {
  refreshSoundSettings();
  window.addEventListener("focus", refreshSoundSettings);
}

// --- temporary mute (per browser) -----------------------------------------
const MUTE_KEY = "alertSoundMutedUntil";

export const getMutedUntil = (): number | null => {
  try {
    const value = Number(window.localStorage.getItem(MUTE_KEY));
    return value > Date.now() ? value : null;
  } catch {
    return null;
  }
};
export const setMutedUntil = (until: number | null) => {
  try {
    if (until) window.localStorage.setItem(MUTE_KEY, String(until));
    else window.localStorage.removeItem(MUTE_KEY);
  } catch {
    // storage unavailable: mute just won't persist
  }
  notify();
};

// Quiet hours are IST 'HH:MM' and may wrap midnight (23:00 -> 07:00).
const inQuietHours = ({ enabled, start, end }: SoundSettings["quiet_hours"]) => {
  if (!enabled) return false;
  const now = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(Date.now());
  return start <= end ? now >= start && now < end : now >= start || now < end;
};

// --- audio unlock (browsers block sound until the page is clicked) -------
export const audioBlocked = () => {
  const context = getContext();
  return !!context && context.state !== "running";
};
export const unlockAudio = () => {
  const context = getContext();
  if (!context) return Promise.resolve();
  return context.resume().catch(() => {});
};

// Several alerts in the same moment play one sound, not a stack of them.
const COOLDOWN_MS = 3000;
let quietUntil = 0;

// News events repeat their sound (Settings → news_event.repeat) so a release is hard to miss.
const repeatsFor = (kind: AlertSoundKind | undefined, from: SoundSettings) =>
  kind === "news_event" ? Math.max(1, Math.min(3, Math.round(from.news_event.repeat ?? 1))) : 1;
const REPEAT_GAP_S = 0.4;
const soundLength = (notes: Note[]) => Math.max(0, ...notes.map((note) => note.at + note.dur));
const repeatNotes = (notes: Note[], times: number): Note[] => {
  const step = soundLength(notes) + REPEAT_GAP_S;
  return Array.from({ length: times }, (_, i) => notes.map((note) => ({ ...note, at: note.at + i * step }))).flat();
};

export const playAlertSound = (kind: AlertSoundKind) => {
  try {
    const choice = settings[kind];
    if (!settings.enabled || !choice.enabled || getMutedUntil() || inQuietHours(settings.quiet_hours)) return;
    const now = Date.now();
    if (now < quietUntil) return;
    const notes = repeatNotes((SOUNDS[choice.sound] ?? SOUNDS[DEFAULT_SETTINGS[kind].sound]).notes, repeatsFor(kind, settings));
    quietUntil = now + Math.max(COOLDOWN_MS, soundLength(notes) * 1000);
    playNotes(notes, settings.volume);
  } catch {
    // Audio is best-effort; never break scanning over it.
  }
};

// Settings-page Test button: ignores mute, quiet hours and the on/off switches.
// Pass the alert kind (and the settings being edited) to hear it as the alert plays it.
export const previewSound = (id: string, volume: number, kind?: AlertSoundKind, from: SoundSettings = settings) => {
  try {
    if (SOUNDS[id]) playNotes(repeatNotes(SOUNDS[id].notes, repeatsFor(kind, from)), volume);
  } catch {
    // best-effort
  }
};
