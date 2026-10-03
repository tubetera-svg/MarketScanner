"""Admin sign-in and the read-only guest role.

Roles
- ``admin``: requests from this machine (loopback, no proxy headers) with no
  login, or any request carrying a valid sign-in token.
- ``guest``: everyone else. Read-only; which pages a guest may open and the
  guest scan limits live in app_settings ``access`` (Settings -> Guest access).

Credentials are stored, never in env or the repo: ``data/state/admin_auth.json``
through ``market_data.state_store`` (a DB row on hosts with APP_STATE_STORE=db)
holds a salted scrypt hash of the admin password plus the random key that signs
tokens. Changing the password replaces the key, which signs out every device.
No password stored = nobody can sign in remotely (hosted site stays read-only).

Setting it: Settings -> Access on localhost (admin without login). To reach the
hosted site, push settings to Turso (Database page -> Turso sync, scope
settings/all): the row is copied like any other data/state file, replacing the
hosted password and signing out hosted devices. Or from a shell that uses the
same DB/env as the API:
    python -m api.auth set-password
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_data import state_store  # noqa: E402
from market_data.timeutil import utc_now  # noqa: E402

AUTH_PATH = ROOT / "data" / "state" / "admin_auth.json"
Role = Literal["admin", "guest"]

MIN_PASSWORD_LENGTH = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_CACHE_SECONDS = 60.0  # the auth row is read at most once a minute (Turso round trip)

# Sign-in throttle: this many failures from one IP locks that IP out for a while.
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 600

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# Any of these means the request came through a proxy/tunnel, so a loopback
# peer address does not prove the user is on this machine.
_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "x-forwarded-host", "true-client-ip")

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "value": None}
_failures: dict[str, tuple[int, float]] = {}  # ip -> (failed attempts, locked-until monotonic)


# --- stored credentials -----------------------------------------------------

def _read() -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        if _cache["value"] is not None and now - _cache["at"] < _CACHE_SECONDS:
            return _cache["value"]
    try:
        value = json.loads(state_store.read_text(AUTH_PATH))
        if not isinstance(value, dict):
            value = {}
    except (OSError, ValueError):
        value = {}
    with _lock:
        _cache.update(at=now, value=value)
    return value


def _write(value: dict[str, Any]) -> None:
    if not state_store.handles(AUTH_PATH):
        AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    state_store.write_text(AUTH_PATH, json.dumps(value, indent=2) + "\n")
    with _lock:
        _cache.update(at=time.monotonic(), value=value)


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)


def password_set() -> bool:
    return bool(_read().get("password_hash"))


def set_password(password: str) -> None:
    """Store a new admin password (hash only) and a new signing key."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(16)
    _write({
        "password_hash": _hash(password, salt).hex(),
        "salt": salt.hex(),
        "secret": secrets.token_hex(32),
        "updated_at": utc_now().isoformat(timespec="seconds"),
    })


def check_password(password: str) -> bool:
    stored = _read()
    if not stored.get("password_hash"):
        return False
    try:
        expected = bytes.fromhex(stored["password_hash"])
        actual = _hash(password, bytes.fromhex(stored["salt"]))
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(expected, actual)


# --- tokens -----------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(days: int) -> str:
    secret = _read().get("secret")
    if not secret:
        raise ValueError("No admin password is set")
    payload = _b64(json.dumps({"exp": int(time.time()) + days * 86400}).encode("utf-8"))
    signature = _b64(hmac.new(bytes.fromhex(secret), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_token(token: str) -> bool:
    secret = _read().get("secret")
    if not secret or token.count(".") != 1:
        return False
    payload, signature = token.split(".")
    expected = _b64(hmac.new(bytes.fromhex(secret), payload.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
        return False
    try:
        return int(json.loads(_unb64(payload))["exp"]) > time.time()
    except (ValueError, KeyError, TypeError):
        return False


# --- requests ---------------------------------------------------------------

def _peer(request: Any) -> str:
    return request.client.host if request.client else ""


def client_ip(request: Any) -> str:
    """Caller identity for per-IP limits. Behind a proxy (Render) every peer is the
    proxy, so use the last X-Forwarded-For entry - the one the proxy appended,
    which a client cannot forge (anything it sends ends up further left)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return _peer(request)


def is_local(request: Any) -> bool:
    """True for a direct request from this machine (no proxy or tunnel in between)."""
    if _peer(request) not in _LOOPBACK:
        return False
    return not any(request.headers.get(name) for name in _PROXY_HEADERS)


def bearer_token(request: Any) -> str:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value[:7].lower() == "bearer " else ""


def role_for(request: Any) -> Role:
    if is_local(request):
        return "admin"
    token = bearer_token(request)
    return "admin" if token and verify_token(token) else "guest"


def login_blocked_for(ip: str) -> int:
    """Seconds this IP must still wait before trying again (0 = may try)."""
    with _lock:
        _count, until = _failures.get(ip, (0, 0.0))
    return max(0, int(until - time.monotonic()))


def record_login(ip: str, ok: bool) -> None:
    with _lock:
        if ok:
            _failures.pop(ip, None)
            return
        count, _until = _failures.get(ip, (0, 0.0))
        count += 1
        until = time.monotonic() + LOCKOUT_SECONDS if count >= MAX_FAILED_LOGINS else 0.0
        _failures[ip] = (0 if until else count, until)


def _cli(argv: list[str]) -> int:
    if argv[:1] != ["set-password"]:
        print("usage: python -m api.auth set-password")
        return 2
    first = getpass.getpass("New admin password: ")
    if first != getpass.getpass("Repeat: "):
        print("Passwords do not match")
        return 1
    try:
        set_password(first)
    except (ValueError, OSError) as exc:
        print(exc)
        return 1
    where = "database (app_state)" if state_store.handles(AUTH_PATH) else str(AUTH_PATH)
    print(f"Admin password saved to {where}; every device is signed out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
