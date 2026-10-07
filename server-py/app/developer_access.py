"""Small, independent access store for the shared-code public API beta.

All writes use SQLite transactions; the market research database is never
modified by registration, key management, or rate limiting.  This is a
single-host beta implementation, not a distributed limiter.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .storage_runtime import is_postgres_path, sync_connect


EMAIL = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,253}$")
SESSION_SECONDS = 7 * 24 * 60 * 60
PER_MINUTE = 10
PER_DAY = 300
MAX_ACTIVE_KEYS = 2
KEY_PREFIX = "cx_trial_"
MAX_NEW_ACCOUNTS_PER_IP_DAY = 5
MAX_NEW_ACCOUNTS_GLOBAL_DAY = 50
_schema_lock = threading.Lock()
_initialized_paths: set[str] = set()


class AccessError(Exception):
    def __init__(self, code: str, status: int = 400, retry_after: int | None = None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)


def _path() -> Path:
    return Path(os.environ.get("DEVELOPER_DB_PATH", "data/developer-access.sqlite"))


def _storage_key() -> str:
    return ('postgres:' if is_postgres_path(_path()) else 'sqlite:')+str(_path().resolve())


@contextmanager
def connection():
    path = _path()
    postgres = is_postgres_path(path)
    if not postgres:
        path.parent.mkdir(parents=True, exist_ok=True)
    # sqlite3 creates files with the process umask; explicitly restrict a new
    # credentials database before opening it.
    if not postgres and not path.exists():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    db = sync_connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        db.close()


def init() -> None:
    path = _storage_key()
    with _schema_lock:
        if path in _initialized_paths:
            return
        with connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
        CREATE TABLE IF NOT EXISTS invitations (
          id TEXT PRIMARY KEY, email TEXT NOT NULL, code_hash TEXT NOT NULL UNIQUE,
          created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          consumed_at INTEGER, consumed_by TEXT
        );
        CREATE INDEX IF NOT EXISTS invitations_email ON invitations(email);
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
          password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
          created_at INTEGER NOT NULL, disabled_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
          csrf_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL, revoked_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
        CREATE TABLE IF NOT EXISTS api_keys (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
          name TEXT NOT NULL, secret_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL, last_used_at INTEGER, revoked_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys(user_id);
        CREATE TABLE IF NOT EXISTS usage_counters (
          user_id TEXT NOT NULL REFERENCES users(id), period TEXT NOT NULL,
          bucket INTEGER NOT NULL, used INTEGER NOT NULL,
          PRIMARY KEY(user_id,period,bucket)
        );
        CREATE TABLE IF NOT EXISTS auth_attempts (
          fingerprint TEXT NOT NULL, bucket INTEGER NOT NULL,
          attempts INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          PRIMARY KEY(fingerprint,bucket)
        );
            """)
            # A trial DB from an earlier local build may predate expiry.
            columns = {row[1] for row in db.execute("PRAGMA table_info(auth_attempts)")}
            if "expires_at" not in columns:
                db.execute("ALTER TABLE auth_attempts ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS auth_attempts_expiry ON auth_attempts(expires_at)")
        _initialized_paths.add(path)


def ready() -> bool:
    init()
    with connection() as db:
        db.execute("SELECT 1 FROM api_keys LIMIT 1").fetchone()
    return True


def _now() -> int:
    return int(time.time())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _email(value: str) -> str:
    email = value.strip().casefold()
    if not EMAIL.fullmatch(email):
        raise AccessError("invalid-email")
    return email


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _shared_invitation_code() -> str:
    code = os.environ.get("DEVELOPER_SHARED_INVITE_CODE", "").strip()
    if not code:
        path = Path(os.environ.get("DEVELOPER_SHARED_INVITE_FILE", "data/developer-shared-invite.txt"))
        try:
            # The local fallback is intended for the production service account,
            # not a public file served by Nginx or included in release archives.
            if path.stat().st_mode & 0o077:
                raise AccessError("registration-unavailable", 503)
            code = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise AccessError("registration-unavailable", 503) from exc
    if not code or len(code) > 128:
        raise AccessError("registration-unavailable", 503)
    return code


def _new_session(db: sqlite3.Connection, user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    # The browser receives this value through a same-origin JSON response.
    # It can be recovered from the opaque session cookie on page refresh.
    csrf = hmac.new(token.encode(), b"cx-trial-csrf-v1", hashlib.sha256).hexdigest()
    now = _now()
    db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,NULL)",
               (_hash(token), user_id, _hash(csrf), now, now + SESSION_SECONDS))
    return token, csrf


def register(email: str, invite_code: str, password: str, client_host: str = "unknown") -> tuple[dict, str, str]:
    init()
    bound = _email(email)
    _admission((("register-ip:" + client_host, 20, 3600),
                ("register-email:" + bound, 8, 3600)))
    if not 12 <= len(password) <= 256:
        raise AccessError("weak-password")
    # The former email-bound, one-time invitation rows are deliberately ignored.
    # Checking the shared code before scrypt makes bad guesses cheap to reject.
    shared_code = _shared_invitation_code()
    if not hmac.compare_digest(invite_code, shared_code):
        raise AccessError("invalid-invitation")
    with connection() as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (bound,)).fetchone():
            raise AccessError("account-exists", 409)
    salt = secrets.token_bytes(16)
    digest = _password_hash(password, salt)
    now = _now()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            # A public shared code cannot enforce the per-account API quota on
            # its own. Bound new accounts per peer and globally until email
            # verification is added; only committed signups count toward caps.
            _auth_limit(db, _hash("register-success-ip:" + client_host), MAX_NEW_ACCOUNTS_PER_IP_DAY, 86400)
            _auth_limit(db, _hash("register-success-global"), MAX_NEW_ACCOUNTS_GLOBAL_DAY, 86400)
            user_id = secrets.token_hex(12)
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,NULL)",
                       (user_id, bound, salt, digest, now))
            token, csrf = _new_session(db, user_id)
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise AccessError("account-exists", 409) from exc
        except BaseException:
            db.rollback()
            raise
    return {"id": user_id, "email": bound}, token, csrf


def _auth_limit(db: sqlite3.Connection, fingerprint: str, max_attempts: int, window: int) -> None:
    now = _now()
    bucket = now // window
    row = db.execute("SELECT attempts FROM auth_attempts WHERE fingerprint=? AND bucket=?",
                     (fingerprint, bucket)).fetchone()
    if row and row["attempts"] >= max_attempts:
        raise AccessError("too-many-attempts", 429, (bucket + 1) * window - now)
    db.execute("INSERT INTO auth_attempts VALUES (?,?,1,?) ON CONFLICT(fingerprint,bucket) DO UPDATE SET attempts=attempts+1,expires_at=excluded.expires_at",
               (fingerprint, bucket, (bucket + 1) * window + 86400))


def _admission(rules: tuple[tuple[str, int, int], ...]) -> None:
    """Atomically count attempts before password hashing or invitation lookup."""
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM auth_attempts WHERE expires_at<?", (_now(),))
            for identity, limit, window in rules:
                _auth_limit(db, _hash(identity), limit, window)
            db.commit()
        except BaseException:
            db.rollback()
            raise


def login(email: str, password: str, client_host: str) -> tuple[dict, str, str]:
    init()
    bound = _email(email)
    # The peer-address limit cannot be bypassed by varying the email. Do not
    # accept spoofable X-Forwarded-For values from arbitrary callers.
    _admission((("login-ip:" + client_host, 30, 900),
                ("login-ip-email:" + client_host + ":" + bound, 8, 900)))
    with connection() as db:
        row = db.execute("SELECT * FROM users WHERE email=? AND disabled_at IS NULL", (bound,)).fetchone()
    # Keep missing-account work comparable to a real password check.
    salt = row["password_salt"] if row else b"\0" * 16
    actual = _password_hash(password, salt)
    if not row or not hmac.compare_digest(actual, row["password_hash"]):
        raise AccessError("invalid-credentials", 401)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            token, csrf = _new_session(db, row["id"])
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return {"id": row["id"], "email": bound}, token, csrf


def session(token: str | None) -> tuple[dict, str] | None:
    if not token or len(token) > 128:
        return None
    init()
    with connection() as db:
        row = db.execute("""SELECT u.id,u.email,s.csrf_hash,s.expires_at
             FROM sessions s JOIN users u ON u.id=s.user_id
             WHERE s.token_hash=? AND s.revoked_at IS NULL AND u.disabled_at IS NULL""",
             (_hash(token),)).fetchone()
    if not row or row["expires_at"] <= _now():
        return None
    csrf = hmac.new(token.encode(), b"cx-trial-csrf-v1", hashlib.sha256).hexdigest()
    if not hmac.compare_digest(_hash(csrf), row["csrf_hash"]):
        return None
    return ({"id": row["id"], "email": row["email"]}, csrf)


def revoke_session(token: str) -> None:
    with connection() as db:
        db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (_now(), _hash(token)))


def list_keys(user_id: str) -> list[dict]:
    with connection() as db:
        rows = db.execute("SELECT id,name,created_at,last_used_at,revoked_at FROM api_keys WHERE user_id=? ORDER BY created_at DESC",
                          (user_id,)).fetchall()
    return [dict(id=r["id"], name=r["name"], prefix=KEY_PREFIX + r["id"],
                 createdAt=r["created_at"] * 1000,
                 lastUsedAt=r["last_used_at"] * 1000 if r["last_used_at"] else None,
                 revokedAt=r["revoked_at"] * 1000 if r["revoked_at"] else None) for r in rows]


def create_key(user_id: str, name: str) -> tuple[dict, str]:
    label = name.strip()
    if not 1 <= len(label) <= 60:
        raise AccessError("invalid-key-name")
    key_id = secrets.token_hex(8)
    secret = KEY_PREFIX + key_id + "." + secrets.token_urlsafe(32)
    now = _now()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            count = db.execute("SELECT count(*) FROM api_keys WHERE user_id=? AND revoked_at IS NULL", (user_id,)).fetchone()[0]
            if count >= MAX_ACTIVE_KEYS:
                raise AccessError("key-limit-reached", 409)
            db.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,NULL,NULL)",
                       (key_id, user_id, label, _hash(secret), now))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return dict(id=key_id, name=label, prefix=KEY_PREFIX + key_id,
                createdAt=now * 1000, lastUsedAt=None, revokedAt=None), secret


def revoke_key(user_id: str, key_id: str) -> bool:
    with connection() as db:
        result = db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                            (_now(), key_id, user_id))
    return result.rowcount > 0


def usage(user_id: str) -> dict:
    now = _now()
    bucket = now // 86400
    with connection() as db:
        row = db.execute("SELECT used FROM usage_counters WHERE user_id=? AND period='day' AND bucket=?",
                         (user_id, bucket)).fetchone()
    used = row["used"] if row else 0
    return {"day": time.strftime("%Y-%m-%d", time.gmtime(now)), "used": used,
            "limit": PER_DAY, "remaining": max(0, PER_DAY - used)}


def authenticate_and_consume(raw_key: str) -> tuple[str, dict]:
    """Validate a key and atomically consume both organization-level quotas."""
    init()
    if len(raw_key) > 128 or not re.fullmatch(r"cx_trial_[0-9a-f]{16}\.[A-Za-z0-9_-]{40,}", raw_key):
        raise AccessError("invalid-api-key", 401)
    key_id = raw_key[len(KEY_PREFIX):].split(".", 1)[0]
    now = _now()
    periods = (("minute", now // 60, PER_MINUTE, 60), ("day", now // 86400, PER_DAY, 86400))
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            key = db.execute("""SELECT k.user_id,k.secret_hash,u.disabled_at FROM api_keys k
                JOIN users u ON u.id=k.user_id WHERE k.id=? AND k.revoked_at IS NULL""", (key_id,)).fetchone()
            if not key or key["disabled_at"] is not None or not hmac.compare_digest(_hash(raw_key), key["secret_hash"]):
                raise AccessError("invalid-api-key", 401)
            used = {}
            for period, bucket, limit, seconds in periods:
                row = db.execute("SELECT used FROM usage_counters WHERE user_id=? AND period=? AND bucket=?",
                                 (key["user_id"], period, bucket)).fetchone()
                count = row["used"] if row else 0
                if count >= limit:
                    raise AccessError("rate-limit-exceeded", 429, (bucket + 1) * seconds - now)
                used[period] = count + 1
            for period, bucket, _, _ in periods:
                db.execute("INSERT INTO usage_counters VALUES (?,?,?,1) ON CONFLICT(user_id,period,bucket) DO UPDATE SET used=used+1",
                           (key["user_id"], period, bucket))
            db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, key_id))
            # Keep only a bounded quota ledger, without request bodies or keys.
            db.execute("DELETE FROM usage_counters WHERE period='minute' AND bucket<?", (now // 60 - 180,))
            db.execute("DELETE FROM usage_counters WHERE period='day' AND bucket<?", (now // 86400 - 90,))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return key["user_id"], {"limit": PER_MINUTE, "remaining": max(0, PER_MINUTE - used["minute"]),
                            "reset": (now // 60 + 1) * 60}
