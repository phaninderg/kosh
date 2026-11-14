"""The people who can sign in to this server, and how they get in.

data/users.json (mode 0600) holds, for each profile, only what is needed to *sign in*: a display name, a role, the profile's
data key wrapped under its password and its recovery key (see finance/vault.py), and public passkey records. It holds no
financial data and nothing that opens a vault without the person's password or recovery key. The admin role can invite,
disable and remove people; it has no way to read anyone's ledger, because the key that opens it is theirs alone.

Codes (the first-run setup code, invite codes, device codes) are random, shown once, and stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import time
import unicodedata
from pathlib import Path

from finance import vault

PASSWORD_MIN = 12
INVITE_TTL = 7 * 24 * 3600
DEVICE_CODE_TTL = 10 * 60
MAX_PASSKEYS = 20
MAX_USERS = 50
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I: read off a screen


class UserError(Exception):
    """A refusal with a message that is safe to show."""


def _digest(code: str) -> str:
    return hashlib.sha256("".join(code.upper().split()).replace("-", "").encode()).hexdigest()


def new_code(length: int = 12) -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
    return "-".join(raw[i:i + 4] for i in range(0, length, 4))


def fold_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().casefold()


class Users:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else vault.DATA_DIR / "users.json"
        self._lock = threading.RLock()

    # storage -------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("users", {})
        data.setdefault("invites", [])
        data.setdefault("links", [])
        data.setdefault("setup", None)
        return data

    def _save(self, data: dict) -> None:
        vault._atomic_write(self.path, json.dumps(data, indent=1).encode())

    # first run -------------------------------------------------------------------

    def has_users(self) -> bool:
        return bool(self._load()["users"])

    def new_setup_code(self) -> str | None:
        """While nobody has signed up, the first profile needs a code that only someone who can read the server's log has
        seen: otherwise whoever reached an internet-facing page first would become the admin."""
        with self._lock:
            data = self._load()
            if data["users"]:
                return None
            code = new_code(12)
            data["setup"] = {"hash": _digest(code), "created": time.time()}
            self._save(data)
            return code

    # sign-up -----------------------------------------------------------------------

    def _check_new(self, data: dict, name: str, password: str) -> str:
        name = " ".join(name.split())
        if not 1 <= len(name) <= 40:
            raise UserError("Choose a name of 1 to 40 characters.")
        if any(fold_name(u["name"]) == fold_name(name) for u in data["users"].values()):
            raise UserError("That name is taken.")
        if len(password) < PASSWORD_MIN:
            raise UserError(f"Use a password of at least {PASSWORD_MIN} characters (a few random words is fine).")
        if len(data["users"]) >= MAX_USERS:
            raise UserError("This server already has the maximum number of profiles.")
        return name

    def sign_up(self, name: str, password: str, code: str) -> tuple[dict, bytes, str]:
        """Create a profile with a setup code (first profile, which becomes the admin) or an invite code.
        Returns (user, data key, recovery key); the recovery key is shown once and never stored."""
        with self._lock:
            data = self._load()
            first = not data["users"]
            digest = _digest(code or "")
            now = time.time()
            if first:
                setup = data["setup"]
                if not setup or not secrets.compare_digest(setup["hash"], digest):
                    raise UserError("That setup code is not right. The server prints it when it starts.")
            else:
                invite = next((i for i in data["invites"] if secrets.compare_digest(i["hash"], digest)
                               and not i.get("used_by") and i["expires"] > now), None)
                if not invite:
                    raise UserError("That invite code is not valid. Ask the admin for a new one.")
            name = self._check_new(data, name, password)
            uid = secrets.token_hex(8)
            dek, keys, recovery = vault.new_key_record(uid, password)
            user = {"id": uid, "name": name, "role": "admin" if first else "member", "created": now, "disabled": False,
                    "sv": 0, "keys": keys, "passkeys": [], "last_seen": None}
            data["users"][uid] = user
            if first:
                data["setup"] = None
            else:
                invite["used_by"], invite["used_at"] = uid, now
            self._save(data)
            return user, dek, recovery

    # lookups --------------------------------------------------------------------------

    def get(self, uid: str) -> dict | None:
        return self._load()["users"].get(uid)

    def all(self) -> list[dict]:
        return sorted(self._load()["users"].values(), key=lambda u: u["created"])

    def by_name(self, name: str) -> dict | None:
        key = fold_name(name)
        return next((u for u in self._load()["users"].values() if fold_name(u["name"]) == key), None)

    def find_passkey(self, credential_id: str) -> tuple[dict, dict] | None:
        for u in self._load()["users"].values():
            for pk in u["passkeys"]:
                if pk["id"] == credential_id:
                    return u, pk
        return None

    def touch(self, uid: str) -> None:
        with self._lock:
            data = self._load()
            if uid in data["users"] and (data["users"][uid].get("last_seen") or 0) < time.time() - 600:
                data["users"][uid]["last_seen"] = time.time()
                self._save(data)

    # passwords and keys -------------------------------------------------------------------

    def open_with_password(self, name: str, password: str) -> tuple[dict, bytes] | None:
        """(user, data key) when the name and password are right and the profile is enabled; None otherwise. An unknown
        name costs the same work as a wrong password, so timing does not reveal which names exist."""
        user = self.by_name(name)
        if not user:
            vault.unlock_with_password("nobody", _DUMMY_KEYS(), password)
            return None
        dek = vault.unlock_with_password(user["id"], user["keys"], password)
        return (user, dek) if dek and not user["disabled"] else None

    def open_with_uid(self, uid: str, password: str) -> bytes | None:
        user = self.get(uid)
        if not user or user["disabled"]:
            return None
        return vault.unlock_with_password(uid, user["keys"], password)

    def change_password(self, uid: str, dek: bytes, new_password: str) -> None:
        if len(new_password) < PASSWORD_MIN:
            raise UserError(f"Use a password of at least {PASSWORD_MIN} characters.")
        with self._lock:
            data = self._load()
            u = data["users"][uid]
            u["keys"] = vault.rewrap_password(uid, u["keys"], dek, new_password)
            u["sv"] += 1                                     # every other session ends
            self._save(data)

    def recover(self, name: str, recovery_key: str, new_password: str) -> tuple[dict, bytes, str] | None:
        """Reset a forgotten password with the recovery key. The old recovery key stops working and a new one is returned."""
        with self._lock:
            data = self._load()
            user = next((u for u in data["users"].values() if fold_name(u["name"]) == fold_name(name)), None)
            if not user:
                vault.unlock_with_recovery("nobody", _DUMMY_KEYS(), recovery_key)
                return None
            dek = vault.unlock_with_recovery(user["id"], user["keys"], recovery_key)
            if not dek or user["disabled"]:
                return None
            if len(new_password) < PASSWORD_MIN:
                raise UserError(f"Use a password of at least {PASSWORD_MIN} characters.")
            keys = vault.rewrap_password(user["id"], user["keys"], dek, new_password)
            user["keys"], new_recovery = vault.rotate_recovery(user["id"], keys, dek)
            user["sv"] += 1
            self._save(data)
            return user, dek, new_recovery

    def new_recovery_key(self, uid: str, dek: bytes) -> str:
        with self._lock:
            data = self._load()
            u = data["users"][uid]
            u["keys"], recovery = vault.rotate_recovery(uid, u["keys"], dek)
            self._save(data)
            return recovery

    def bump_sessions(self, uid: str) -> None:
        """Sign this person out of every device."""
        with self._lock:
            data = self._load()
            if uid in data["users"]:
                data["users"][uid]["sv"] += 1
                self._save(data)

    # invites (admin) --------------------------------------------------------------------------

    def make_invite(self, note: str = "") -> str:
        with self._lock:
            data = self._load()
            now = time.time()
            data["invites"] = [i for i in data["invites"] if i.get("used_by") or i["expires"] > now][-100:]
            code = new_code(12)
            data["invites"].append({"hash": _digest(code), "note": note.strip()[:60], "created": now,
                                    "expires": now + INVITE_TTL, "used_by": None})
            self._save(data)
            return code

    def invites(self) -> list[dict]:
        now = time.time()
        return [{"hash": i["hash"][:12], "note": i["note"], "created": i["created"], "expires": i["expires"],
                 "state": "used" if i.get("used_by") else "expired" if i["expires"] <= now else "open"}
                for i in self._load()["invites"]]

    def revoke_invite(self, short_hash: str) -> None:
        with self._lock:
            data = self._load()
            data["invites"] = [i for i in data["invites"] if not i["hash"].startswith(short_hash) or i.get("used_by")]
            self._save(data)

    # admin ----------------------------------------------------------------------------------------

    def admins(self) -> list[dict]:
        return [u for u in self.all() if u["role"] == "admin" and not u["disabled"]]

    def set_disabled(self, uid: str, disabled: bool) -> None:
        with self._lock:
            data = self._load()
            u = data["users"].get(uid)
            if not u:
                raise UserError("No such profile.")
            if disabled and u["role"] == "admin" and len([a for a in data["users"].values() if a["role"] == "admin" and not a["disabled"]]) <= 1:
                raise UserError("You cannot disable the only admin.")
            u["disabled"] = disabled
            u["sv"] += 1
            self._save(data)

    def delete(self, uid: str) -> None:
        """Remove a profile and everything sealed in its vault. This cannot be undone."""
        with self._lock:
            data = self._load()
            u = data["users"].get(uid)
            if not u:
                raise UserError("No such profile.")
            if u["role"] == "admin" and len([a for a in data["users"].values() if a["role"] == "admin"]) <= 1:
                raise UserError("You cannot remove the only admin.")
            del data["users"][uid]
            self._save(data)
        root = vault.USERS_DIR / uid
        if root.is_dir() and root.resolve().parent == vault.USERS_DIR.resolve():
            shutil.rmtree(root, ignore_errors=True)

    # passkeys ---------------------------------------------------------------------------------------

    def add_passkey(self, uid: str, credential_id: str, public_key: bytes, sign_count: int, transports: list[str], name: str) -> None:
        with self._lock:
            data = self._load()
            u = data["users"][uid]
            if len(u["passkeys"]) >= MAX_PASSKEYS:
                raise UserError("This profile already has the maximum number of passkeys.")
            if any(pk["id"] == credential_id for x in data["users"].values() for pk in x["passkeys"]):
                raise UserError("That passkey is already registered.")
            u["passkeys"].append({"id": credential_id, "public_key": vault._b64(public_key), "sign_count": sign_count,
                                  "transports": transports, "name": (name or "Passkey").strip()[:40], "created": time.time(),
                                  "last_used": None})
            self._save(data)

    def update_passkey_counter(self, uid: str, credential_id: str, sign_count: int) -> None:
        with self._lock:
            data = self._load()
            for pk in data["users"][uid]["passkeys"]:
                if pk["id"] == credential_id:
                    pk["sign_count"], pk["last_used"] = sign_count, time.time()
            self._save(data)

    def remove_passkey(self, uid: str, credential_id: str) -> None:
        with self._lock:
            data = self._load()
            u = data["users"][uid]
            u["passkeys"] = [pk for pk in u["passkeys"] if pk["id"] != credential_id]
            self._save(data)

    # device codes: let a second device add its own passkey without the password -------------------------------

    def make_device_code(self, uid: str) -> str:
        with self._lock:
            data = self._load()
            now = time.time()
            data["links"] = [x for x in data["links"] if x["expires"] > now and x["uid"] != uid]    # a newer code voids the old
            code = new_code(12)
            data["links"].append({"hash": _digest(code), "uid": uid, "expires": now + DEVICE_CODE_TTL})
            self._save(data)
            return code

    def find_device_code(self, code: str) -> dict | None:
        digest, now = _digest(code or ""), time.time()
        link = next((x for x in self._load()["links"] if secrets.compare_digest(x["hash"], digest) and x["expires"] > now), None)
        return self.get(link["uid"]) if link else None

    def burn_device_code(self, code: str) -> None:
        with self._lock:
            data = self._load()
            digest = _digest(code or "")
            data["links"] = [x for x in data["links"] if x["hash"] != digest]
            self._save(data)


def _DUMMY_KEYS() -> dict:
    """Key records for a name that does not exist, so a miss does the same scrypt work as a hit."""
    global _dummy
    if _dummy is None:
        _, _dummy, _ = vault.new_key_record("nobody", "unused unused unused")
    return _dummy


_dummy: dict | None = None
