"use client";

// Header mute button: temporarily silence alert sounds in this browser.
// Toasts and desktop notifications still show while muted.

import { useEffect, useRef, useState } from "react";
import { Volume2, VolumeX } from "lucide-react";
import { getMutedUntil, getSoundSettings, setMutedUntil, subscribeSoundSettings } from "./alertSound";
import { getDisplayTimezone, nextWallTime, useDisplayTimezone, zoneLabel } from "./time";

// Next 09:00 in the display timezone after now.
const next9am = () => nextWallTime(9, 0, getDisplayTimezone());

const remaining = (until: number) => {
  const minutes = Math.max(1, Math.round((until - Date.now()) / 60000));
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
};

export default function SoundToggle() {
  const displayTz = useDisplayTimezone();
  const [mutedUntil, setMuted] = useState<number | null>(null);
  const [soundsOn, setSoundsOn] = useState(true);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const sync = () => {
      setMuted(getMutedUntil());
      setSoundsOn(getSoundSettings().enabled);
    };
    sync();
    const unsubscribe = subscribeSoundSettings(sync);
    const id = window.setInterval(sync, 30000); // expire the mute / refresh the countdown
    return () => { unsubscribe(); window.clearInterval(id); };
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const mute = (until: number | null) => {
    setMutedUntil(until);
    setOpen(false);
  };

  const silent = !soundsOn || mutedUntil !== null;
  const title = !soundsOn ? "Alert sounds are off in Settings" : mutedUntil ? `Sounds muted for ${remaining(mutedUntil)}` : "Alert sounds on — click to mute";
  const option = { display: "block", width: "100%", textAlign: "left" as const, padding: "6px 10px", border: 0, background: "transparent", color: "var(--ink)", font: "11px 'DM Mono', monospace", cursor: "pointer" };

  return (
    <span ref={ref} style={{ position: "relative", display: "inline-flex" }}>
      <button type="button" className="top-link" title={title} aria-label={title} aria-expanded={open} onClick={() => setOpen((v) => !v)}
        style={{ background: "transparent", cursor: "pointer", color: silent ? "var(--coral)" : undefined }}>
        {silent ? <VolumeX size={13} /> : <Volume2 size={13} />}
        {mutedUntil ? remaining(mutedUntil) : null}
      </button>
      {open && (
        <div role="menu" style={{ position: "absolute", top: "calc(100% + 4px)", right: 0, zIndex: 50, minWidth: 170, background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 7, boxShadow: "0 6px 20px rgba(0,0,0,.12)", padding: "4px 0" }}>
          {!soundsOn && <div style={{ ...option, color: "var(--muted)", cursor: "default" }}>Sounds are off in Settings</div>}
          {mutedUntil ? (
            <button type="button" role="menuitem" style={option} onClick={() => mute(null)}>Unmute</button>
          ) : null}
          <button type="button" role="menuitem" style={option} onClick={() => mute(Date.now() + 15 * 60000)}>Mute 15 min</button>
          <button type="button" role="menuitem" style={option} onClick={() => mute(Date.now() + 60 * 60000)}>Mute 1 hour</button>
          <button type="button" role="menuitem" style={option} onClick={() => mute(next9am())}>Mute until 09:00 {zoneLabel(displayTz)}</button>
        </div>
      )}
    </span>
  );
}
