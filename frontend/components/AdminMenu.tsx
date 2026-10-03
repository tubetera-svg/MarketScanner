"use client";

// Header lock: guests can sign in as admin; a signed-in admin can sign out.
// On localhost the API makes every request admin, so there is nothing to do.

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Lock, LockOpen } from "lucide-react";
import { signIn, signOut, useAuth } from "./auth";

export default function AdminMenu() {
  const auth = useAuth();
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  if (!auth) return null;
  const admin = auth.role === "admin";
  const title = admin ? (auth.local ? "Admin (localhost - no sign-in needed)" : "Signed in as admin") : "Read-only guest - sign in as admin";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await signIn(password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
      setBusy(false);
    }
  };

  const panel = { position: "absolute" as const, top: "calc(100% + 4px)", right: 0, zIndex: 50, width: 230, background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 7, boxShadow: "0 6px 20px rgba(0,0,0,.12)", padding: 10, font: "11px 'DM Mono', monospace" };

  return (
    <span ref={ref} style={{ position: "relative", display: "inline-flex", alignItems: "center", gap: 6 }}>
      {!admin && auth.access.guest_banner && (
        <span title="Read-only guest access" style={{ font: "10px 'DM Mono', monospace", color: "var(--slate-ink)", background: "var(--slate-soft)", borderRadius: 10, padding: "3px 8px", whiteSpace: "nowrap" }}>
          {auth.access.guest_banner}
        </span>
      )}
      <button type="button" className="top-link" title={title} aria-label={title} aria-expanded={open} onClick={() => setOpen((v) => !v)}
        style={{ background: "transparent", cursor: "pointer", color: admin ? "var(--teal)" : undefined }}>
        {admin ? <LockOpen size={13} /> : <Lock size={13} />}
      </button>
      {open && (
        <div role="dialog" aria-label="Admin sign-in" style={panel}>
          {admin ? (
            <>
              <div style={{ color: "var(--muted)", marginBottom: auth.local ? 0 : 8 }}>{title}</div>
              {!auth.local && <button type="button" className="chart-tool-btn" onClick={signOut}>Sign out</button>}
            </>
          ) : !auth.password_set ? (
            <div style={{ color: "var(--muted)", lineHeight: 1.5 }}>No admin password is set yet. Set one in Settings › Access on localhost, then push settings to Turso.</div>
          ) : (
            <form onSubmit={submit} style={{ display: "grid", gap: 6 }}>
              <label htmlFor="admin-password" style={{ color: "var(--muted)" }}>Admin password</label>
              <input id="admin-password" type="password" autoComplete="current-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)}
                style={{ height: 28, border: "1px solid var(--line)", borderRadius: 4, padding: "0 8px", font: "12px 'DM Mono', monospace" }} />
              {error && <div role="alert" style={{ color: "var(--coral-ink)" }}>{error}</div>}
              <button type="submit" className="chart-tool-btn primary" disabled={busy || !password}>{busy ? "Signing in…" : "Sign in"}</button>
            </form>
          )}
        </div>
      )}
    </span>
  );
}
