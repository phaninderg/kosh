"""Per-profile encrypted storage.

Every profile owns one random 256-bit data key (the DEK). Everything that profile stores (the ledger, saved secrets,
mailbox settings, uploaded statements, job logs, backups) is sealed with AES-256-GCM under that key. The key itself is
never stored in the clear: it is kept only *wrapped* (encrypted) under keys derived from the person's password and from a
one-time recovery key, using scrypt. So

  * the admin, other profiles, and anyone who copies the data folder or a backup get ciphertext they cannot open;
  * the right password both proves who you are and unwraps the key (a wrong password fails the GCM check);
  * the unwrapped key lives only in the server's memory while the person is unlocked, and is dropped on lock, idle
    timeout or restart.

This protects data at rest and from other profiles. It cannot protect against someone who can read the server's memory
while a profile is unlocked (that is true of every server-side design).

File layout of one profile (all names are opaque, all contents sealed):  data/users/<uid>/ledger.enc, f/<hmac>.enc
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import weakref
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USERS_DIR = DATA_DIR / "users"
KDF = {"n": 2**15, "r": 8, "p": 1}                 # tests lower this; the stored record carries its own parameters
MAGIC = b"KOSH1"
VAULT_DB = "vault:db"                              # what a db path looks like when it means "the signed-in profile's ledger"
VAULT_FILES = "vault:files"                        # likewise for the mailbox settings / statement inbox
IDLE_LOCK_SECONDS = int(os.getenv("KOSH_IDLE_LOCK_HOURS", "8")) * 3600


class VaultError(Exception):
    """Wrong key, damaged file, or a vault that is not unlocked."""


class VaultLocked(VaultError):
    """No unlocked profile in this context."""


class VaultClosed(VaultLocked):
    """This Vault object was locked (or its profile removed) and must not be used again."""


class VaultBusy(VaultError):
    """The profile's ledger is being written by something else (for example a running sync)."""


# ── sealing ───────────────────────────────────────────────────────────────────

def seal(key: bytes, data: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, aad)


def unseal(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 12 + 16:
        raise VaultError("not a Kosh sealed file")
    try:
        return AESGCM(key).decrypt(blob[len(MAGIC):len(MAGIC) + 12], blob[len(MAGIC) + 12:], aad)
    except InvalidTag:
        raise VaultError("wrong key or damaged file") from None


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def _kek(secret: str, salt: bytes, kdf: dict) -> bytes:
    return hashlib.scrypt(secret.encode(), salt=salt, n=kdf["n"], r=kdf["r"], p=kdf["p"], maxmem=128 * 1024 * 1024, dklen=32)


# ── key records: how a data key is stored on disk (always wrapped) ──────────────────────

_RECOVERY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def new_recovery_key() -> str:
    """160 random bits as 8 groups of 4 characters, e.g. ABCD-EFGH-...: shown once, never stored."""
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(32))
    return "-".join(raw[i:i + 4] for i in range(0, 32, 4))


def _clean_recovery(key: str) -> str:
    return re.sub(r"[^A-Z2-7]", "", key.upper())


def _wrap(dek: bytes, secret: str, kdf: dict, aad: bytes) -> dict:
    salt = secrets.token_bytes(16)
    return {"kdf": dict(kdf), "salt": _b64(salt), "wrapped": _b64(seal(_kek(secret, salt, kdf), dek, aad))}


def _unwrap(entry: dict, secret: str, aad: bytes) -> bytes | None:
    try:
        return unseal(_kek(secret, _unb64(entry["salt"]), entry["kdf"]), _unb64(entry["wrapped"]), aad)
    except (VaultError, KeyError, ValueError):
        return None


def new_key_record(uid: str, password: str) -> tuple[bytes, dict, str]:
    """(data key, record to store, recovery key to show once)."""
    dek = secrets.token_bytes(32)
    recovery = new_recovery_key()
    record = {"password": _wrap(dek, password, KDF, b"pw:" + uid.encode()),
              "recovery": _wrap(dek, _clean_recovery(recovery), KDF, b"rk:" + uid.encode())}
    return dek, record, recovery


def unlock_with_password(uid: str, record: dict, password: str) -> bytes | None:
    return _unwrap(record["password"], password, b"pw:" + uid.encode())


def unlock_with_recovery(uid: str, record: dict, recovery_key: str) -> bytes | None:
    return _unwrap(record["recovery"], _clean_recovery(recovery_key), b"rk:" + uid.encode())


def rewrap_password(uid: str, record: dict, dek: bytes, new_password: str) -> dict:
    return {**record, "password": _wrap(dek, new_password, KDF, b"pw:" + uid.encode())}


def rotate_recovery(uid: str, record: dict, dek: bytes) -> tuple[dict, str]:
    recovery = new_recovery_key()
    return {**record, "recovery": _wrap(dek, _clean_recovery(recovery), KDF, b"rk:" + uid.encode())}, recovery


# ── the encrypted database ───────────────────────────────────────────────────────

_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _write_lock_for(uid: str) -> threading.Lock:
    """One write lock per profile for the life of the process, shared by every Vault object made for it, so locking and
    unlocking again can never produce two writers."""
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(uid, threading.Lock())


class _WriteToken:
    """Holds the ledger's write lock for one connection and gives it back exactly once, even if the connection is
    garbage-collected without close()."""

    def __init__(self, lock: threading.Lock):
        self._lock, self._held, self._mutex = lock, True, threading.Lock()

    def release(self) -> None:
        with self._mutex:
            if self._held:
                self._held = False
                self._lock.release()


class VaultConnection(sqlite3.Connection):
    """An in-memory SQLite connection whose every commit is re-sealed to disk. Created only by Vault.connect()."""

    _vault: "Vault"
    _writer = False
    _token: _WriteToken | None = None
    _persisted_changes = 0

    def commit(self):
        super().commit()
        if self._writer:
            self._vault._persist(self)

    def __exit__(self, *exc):                          # `with conn:` commits inside the C layer, not through commit()
        result = super().__exit__(*exc)
        if self._writer and exc[0] is None:
            self._vault._persist(self)
        return result

    def close(self):
        try:
            if self._writer and self.total_changes != self._persisted_changes and not self.in_transaction:
                self._vault._persist(self)                 # a change that was never explicitly committed still lands
        finally:
            self._writer = False
            super().close()
            if self._token:
                self._token.release()


class Vault:
    """One unlocked profile: its data key and the files sealed under it."""

    WRITE_WAIT = 3                                     # seconds a writer waits for another writer (a sync) before saying "busy"

    def __init__(self, uid: str, dek: bytes, root: Path | None = None):
        self.uid, self._dek = uid, dek
        self.root = Path(root) if root else USERS_DIR / uid
        self._lock = threading.RLock()
        self._write_lock = _write_lock_for(uid)
        self._closed = False
        self._master: bytes | None = None
        self._loaded = False

    def close(self) -> None:
        """Lock this vault for good: forget the key and refuse every later read, write or commit, including from a job that is
        still running. Unlocking again makes a new Vault."""
        with self._lock:
            self._closed, self._dek, self._master = True, None, None

    def _check(self) -> None:
        if self._closed:
            raise VaultClosed("this profile was locked")

    def hash_key(self) -> bytes:
        self._check()
        return hmac.new(self._dek, b"kosh-account-hash-key", hashlib.sha256).digest()

    # sealed files ------------------------------------------------------------

    def _path(self, name: str) -> Path:
        tag = hmac.new(self._dek, b"name:" + name.encode(), hashlib.sha256).hexdigest()[:40]
        return self.root / "f" / f"{tag}.enc"

    def put(self, name: str, data: bytes) -> None:
        with self._lock:
            self._check()
            path = self._path(name)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            header = json.dumps({"name": name}).encode()
            _atomic_write(path, seal(self._dek, len(header).to_bytes(2, "big") + header + data, aad=b"file"))

    def get(self, name: str) -> bytes | None:
        with self._lock:
            self._check()
            try:
                blob = self._path(name).read_bytes()
            except OSError:
                return None
            stored, data = _split(unseal(self._dek, blob, aad=b"file"))
            if stored != name:                                     # a file moved under another name is not trusted
                raise VaultError("sealed file does not belong to that name")
            return data

    def delete(self, name: str) -> None:
        with self._lock:
            self._check()
            self._path(name).unlink(missing_ok=True)

    def names(self, prefix: str = "") -> list[str]:
        out = []
        with self._lock:
            self._check()
            for p in sorted((self.root / "f").glob("*.enc")):
                try:
                    name = _split(unseal(self._dek, p.read_bytes(), aad=b"file"))[0]
                except (VaultError, OSError):
                    continue
                if name.startswith(prefix):
                    out.append(name)
        return sorted(out)

    # the ledger -----------------------------------------------------------------

    @property
    def _db_file(self) -> Path:
        return self.root / "ledger.enc"

    def _master_bytes(self) -> bytes | None:
        with self._lock:
            if not self._loaded:
                try:
                    self._master = unseal(self._dek, self._db_file.read_bytes(), aad=b"ledger")
                except OSError:
                    self._master = None
                self._loaded = True
            return self._master

    def connect(self, writable: bool) -> VaultConnection:
        """A private in-memory copy of the ledger. A writer holds the write lock until it closes."""
        self._check()
        if writable and not self._write_lock.acquire(timeout=self.WRITE_WAIT):
            raise VaultBusy("the ledger is busy (a sync or import is writing); try again in a moment")
        try:
            conn = sqlite3.connect(":memory:", factory=VaultConnection, check_same_thread=False)
            conn._vault, conn._writer = self, writable
            if writable:
                conn._token = _WriteToken(self._write_lock)
                weakref.finalize(conn, conn._token.release)
            master = self._master_bytes()
            if master:
                conn.deserialize(master)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn._persisted_changes = conn.total_changes
            return conn
        except BaseException:
            if writable:
                self._write_lock.release()
            raise

    def _persist(self, conn: VaultConnection) -> None:
        data = sqlite3.Connection.serialize(conn)
        with self._lock:
            self._check()                                  # a job still running after a lock, disable or delete cannot write
            if data != self._master:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                _atomic_write(self._db_file, seal(self._dek, data, aad=b"ledger"))
                self._master = data
        conn._persisted_changes = conn.total_changes

    def snapshot(self, directory: Path | None = None) -> Path:
        """A timestamped copy of the sealed ledger (already encrypted, so it is safe to store anywhere)."""
        with self._lock:
            self._check()
            directory = Path(directory) if directory else self.root / "backups"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest = directory / f"ledger-{time.strftime('%Y%m%d-%H%M%S')}.enc"
            self._master_bytes()
            if self._master is not None:
                _atomic_write(dest, seal(self._dek, self._master, aad=b"ledger"))
            return dest


def _split(payload: bytes) -> tuple[str, bytes]:
    n = int.from_bytes(payload[:2], "big")
    return json.loads(payload[2:2 + n])["name"], payload[2 + n:]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")      # never shared between writers
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ── who is unlocked right now ────────────────────────────────────────────────────

current_vault: contextvars.ContextVar[Vault | None] = contextvars.ContextVar("kosh_vault", default=None)


def require_vault() -> Vault:
    v = current_vault.get()
    if v is None:
        raise VaultLocked("no unlocked profile")
    return v


class Registry:
    """Unlocked vaults held in memory, dropped after `idle` seconds without a person using them.

    The time of last use lives here, not on the Vault, and only get() and unlock() refresh it. Background work (a mail sync,
    the scheduler's peek_all) works on the Vault directly and has no way to touch the timer, so it cannot keep a key in memory
    longer than the person chose."""

    def __init__(self, idle: int | None = None, clock=time.time):
        self._vaults: dict[str, Vault] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.idle, self._clock = idle if idle is not None else IDLE_LOCK_SECONDS, clock

    def unlock(self, uid: str, dek: bytes) -> Vault:
        with self._lock:
            vault = self._vaults.get(uid) or self._vaults.setdefault(uid, Vault(uid, dek))
            self._last[uid] = self._clock()
            return vault

    def _expire(self, uid: str) -> bool:
        """Lock `uid` if it has been idle too long (caller holds the lock). True if it was locked."""
        if uid in self._vaults and self._clock() - self._last[uid] > self.idle:
            self._vaults.pop(uid).close()
            self._last.pop(uid, None)
            return True
        return False

    def get(self, uid: str) -> Vault | None:
        """The unlocked vault for a person's own request: this counts as use and restarts the idle timer."""
        with self._lock:
            if self._expire(uid) or uid not in self._vaults:
                return None
            self._last[uid] = self._clock()
            return self._vaults[uid]

    def peek_all(self) -> list[Vault]:
        """Every vault unlocked right now, for background work. Does not count as use; locks any that are already idle."""
        with self._lock:
            for uid in list(self._vaults):
                self._expire(uid)
            return list(self._vaults.values())

    def lock(self, uid: str) -> None:
        with self._lock:
            v = self._vaults.pop(uid, None)
            self._last.pop(uid, None)
        if v:
            v.close()

    def unlocked(self, uid: str) -> bool:
        return self.get(uid) is not None
