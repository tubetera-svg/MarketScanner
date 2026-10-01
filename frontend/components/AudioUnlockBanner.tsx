"use client";

// Browsers keep audio suspended until the page is clicked, so the first alert
// after a fresh load could be silent. Show a hint until the first interaction.

import { useEffect, useState } from "react";
import { audioBlocked, getSoundSettings, subscribeSoundSettings, unlockAudio } from "./alertSound";

export default function AudioUnlockBanner() {
  const [blocked, setBlocked] = useState(false);
  const [soundsOn, setSoundsOn] = useState(true);

  useEffect(() => {
    const syncSettings = () => setSoundsOn(getSoundSettings().enabled);
    syncSettings();
    const unsubscribe = subscribeSoundSettings(syncSettings);
    // Give an already-allowed context a moment to reach "running".
    const timer = window.setTimeout(() => setBlocked(audioBlocked()), 500);
    const onInteract = () => {
      void unlockAudio().then(() => setBlocked(audioBlocked()));
    };
    window.addEventListener("pointerdown", onInteract);
    window.addEventListener("keydown", onInteract);
    return () => {
      unsubscribe();
      window.clearTimeout(timer);
      window.removeEventListener("pointerdown", onInteract);
      window.removeEventListener("keydown", onInteract);
    };
  }, []);

  if (!blocked || !soundsOn) return null;
  return (
    <div role="status" style={{ position: "fixed", top: 0, left: "50%", transform: "translateX(-50%)", zIndex: 60, marginTop: 6, padding: "5px 12px", borderRadius: 7, background: "var(--coral-soft)", color: "var(--coral-ink)", border: "1px solid var(--coral)", font: "11px 'DM Mono', monospace", cursor: "pointer" }}>
      🔇 Sounds are blocked by the browser — click anywhere to enable
    </div>
  );
}
