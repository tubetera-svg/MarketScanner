"use client";

import { useEffect, useState } from "react";

/**
 * Drives the status pill's attention pop: returns true for `durationMs` every
 * time `message` changes, so EVERY status line (scan, sync, errors, Silver
 * Bullet, …) gets the same treatment instead of one special case.
 *
 * Pair with the `.status-flash` class on the `.status` element.
 */
export function useStatusFlash(message: string, durationMs = 2200) {
  const [flashing, setFlashing] = useState(false);

  useEffect(() => {
    setFlashing(true);
    const timer = window.setTimeout(() => setFlashing(false), durationMs);
    return () => window.clearTimeout(timer);
  }, [message, durationMs]);

  return flashing;
}
