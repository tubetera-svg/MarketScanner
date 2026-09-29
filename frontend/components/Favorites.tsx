"use client";

import { useCallback, useEffect, useState, type MouseEvent } from "react";
import { Star } from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/**
 * Starred symbols shared by the scanner, Watchlist and IPO pages
 * (config/favorites.json via /api/favorites). Toggles are optimistic and
 * rolled back if the server rejects them.
 */
export function useFavorites() {
  const [favorites, setFavorites] = useState<Set<string>>(new Set());

  // Re-read after a delete / rename: the server drops or moves those stars.
  const reload = useCallback(async () => {
    try {
      const response = await fetch(`${API}/api/favorites`, { cache: "no-store" });
      const payload = response.ok ? await response.json() : null;
      if (payload?.symbols) setFavorites(new Set(payload.symbols as string[]));
    } catch {
      // keep the current set
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const isFavorite = useCallback((symbol: string) => favorites.has(symbol.toUpperCase()), [favorites]);

  const toggle = useCallback(async (symbol: string) => {
    const key = symbol.toUpperCase();
    const adding = !favorites.has(key);
    const flip = (current: Set<string>, add: boolean) => {
      const next = new Set(current);
      if (add) next.add(key); else next.delete(key);
      return next;
    };
    setFavorites((current) => flip(current, adding));
    try {
      const response = await fetch(`${API}/api/favorites`, {
        method: adding ? "POST" : "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: key }),
      });
      if (!response.ok) throw new Error(String(response.status));
      const payload = await response.json();
      if (payload?.symbols) setFavorites(new Set(payload.symbols as string[]));
    } catch {
      setFavorites((current) => flip(current, !adding));
    }
  }, [favorites]);

  return { favorites, isFavorite, toggle, reload };
}

export function FavoriteStar({ active, onToggle, symbol }: { active: boolean; onToggle: () => void; symbol: string }) {
  const handle = (event: MouseEvent<HTMLButtonElement>) => {
    // Rows are often labels / clickable; don't let the star also toggle them.
    event.preventDefault();
    event.stopPropagation();
    onToggle();
  };
  return (
    <button
      type="button"
      className={`fav-star${active ? " active" : ""}`}
      aria-pressed={active}
      aria-label={active ? `Remove ${symbol} from favorites` : `Add ${symbol} to favorites`}
      title={active ? "Remove from favorites" : "Add to favorites"}
      onClick={handle}
    >
      <Star size={13} fill={active ? "currentColor" : "none"} />
    </button>
  );
}
