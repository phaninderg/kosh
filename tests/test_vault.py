"""The encryption layer: sealed files, wrapped keys, and the encrypted ledger."""

import gc
import sqlite3

import pytest

from finance import vault
from finance.db import connect_ro, connect_rw

PW = "a perfectly good passphrase"


def test_sealing_round_trips_and_hides_the_content():
    key = b"k" * 32
    blob = vault.seal(key, b"balance 12,34,567", aad=b"x")
    assert b"balance" not in blob and blob.startswith(vault.MAGIC)
    assert vault.unseal(key, blob, aad=b"x") == b"balance 12,34,567"
    assert vault.seal(key, b"same", b"") != vault.seal(key, b"same", b"")          # a fresh nonce every time


@pytest.mark.parametrize("damage", ["wrong key", "wrong aad", "flipped byte", "truncated", "not ours"])
def test_a_sealed_file_that_is_wrong_in_any_way_is_refused(damage):
    key, blob = b"k" * 32, vault.seal(b"k" * 32, b"secret", aad=b"a")
    args = {"wrong key": (b"j" * 32, blob, b"a"), "wrong aad": (key, blob, b"b"),
            "flipped byte": (key, blob[:-1] + bytes([blob[-1] ^ 1]), b"a"), "truncated": (key, blob[:20], b"a"),
            "not ours": (key, b"SQLite format 3\x00" + bytes(50), b"a")}[damage]
    with pytest.raises(vault.VaultError):
        vault.unseal(*args)


def test_the_data_key_is_only_ever_stored_wrapped_and_opens_with_the_password_or_the_recovery_key():
    dek, record, recovery = vault.new_key_record("u1", PW)
    assert dek not in str(record).encode() and PW not in str(record) and recovery not in str(record)
    assert vault.unlock_with_password("u1", record, PW) == dek
    assert vault.unlock_with_password("u1", record, PW + "x") is None
    assert vault.unlock_with_password("u2", record, PW) is None                   # bound to this profile
    assert vault.unlock_with_recovery("u1", record, recovery) == dek
    assert vault.unlock_with_recovery("u1", record, recovery.lower().replace("-", " ")) == dek     # forgiving about format
    assert vault.unlock_with_recovery("u1", record, "AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA") is None
    assert len(recovery.replace("-", "")) == 32


def test_changing_the_password_keeps_the_data_key_and_retires_the_old_password():
    dek, record, recovery = vault.new_key_record("u1", PW)
    again = vault.rewrap_password("u1", record, dek, "a brand new passphrase")
    assert vault.unlock_with_password("u1", again, "a brand new passphrase") == dek
    assert vault.unlock_with_password("u1", again, PW) is None
    assert vault.unlock_with_recovery("u1", again, recovery) == dek


def test_a_new_recovery_key_replaces_the_old_one():
    dek, record, old = vault.new_key_record("u1", PW)
    record, new = vault.rotate_recovery("u1", record, dek)
    assert vault.unlock_with_recovery("u1", record, new) == dek and vault.unlock_with_recovery("u1", record, old) is None
    assert vault.unlock_with_password("u1", record, PW) == dek


# ── the encrypted ledger ─────────────────────────────────────────────────────

def test_the_ledger_on_disk_is_ciphertext(db_path):
    conn = connect_rw(db_path)
    conn.execute("INSERT INTO accounts (type, institution, name, mask, ref_hash) VALUES ('bank','ZZ Secret Bank','ZZ Secret Bank …1234','1234','h')")
    conn.commit(); conn.close()
    v = vault.current_vault.get()
    raw = (v.root / "ledger.enc").read_bytes()
    assert raw.startswith(vault.MAGIC) and b"SQLite format" not in raw and b"ZZ Secret Bank" not in raw
    assert b"ZZ Secret Bank" not in b"".join(p.read_bytes() for p in v.root.rglob("*") if p.is_file())


def test_writes_survive_a_restart_with_the_same_key_and_not_with_another(me, users, registry):
    conn = connect_rw(vault.VAULT_DB)
    with conn:
        conn.execute("INSERT INTO accounts (type, institution, name, mask, ref_hash) VALUES ('bank','B','B …1','1','h1')")
    conn.close()
    v = vault.current_vault.get()
    reopened = vault.Vault(me["id"], v._dek)                                       # as after a server restart
    token = vault.current_vault.set(reopened)
    try:
        assert connect_ro(vault.VAULT_DB).execute("select count(*) from accounts").fetchone()[0] == 1
    finally:
        vault.current_vault.reset(token)
    wrong = vault.Vault(me["id"], b"x" * 32)
    token = vault.current_vault.set(wrong)
    try:
        with pytest.raises(vault.VaultError):
            connect_ro(vault.VAULT_DB)
    finally:
        vault.current_vault.reset(token)


def test_a_change_that_was_never_committed_is_not_kept(db_path):
    conn = connect_rw(db_path)
    conn.execute("INSERT INTO accounts (type, institution, name, mask, ref_hash) VALUES ('bank','B','B …1','1','h1')")
    conn.rollback(); conn.close()
    assert connect_ro(db_path).execute("select count(*) from accounts").fetchone()[0] == 0


def test_readers_see_a_private_copy_and_cannot_write(db_path):
    connect_rw(db_path).close()
    ro = connect_ro(db_path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO accounts (type, institution, name, mask, ref_hash) VALUES ('bank','B','B','1','h')")


def test_a_second_writer_waits_then_reports_busy(db_path, monkeypatch):
    first = connect_rw(db_path)
    monkeypatch.setattr(vault.Vault, "WRITE_WAIT", 0.05)
    with pytest.raises(vault.VaultBusy):
        connect_rw(db_path)
    first.close()
    connect_rw(db_path).close()                                                    # the lock came back


def test_a_writer_dropped_without_close_gives_the_lock_back(db_path):
    connect_rw(db_path)                                                            # never closed
    gc.collect()
    connect_rw(db_path).close()


def test_a_ledger_backup_is_sealed_and_can_be_opened_only_by_its_owner(db_path, me):
    conn = connect_rw(db_path)
    with conn:
        conn.execute("INSERT INTO accounts (type, institution, name, mask, ref_hash) VALUES ('bank','B','B …1','1','h1')")
    conn.close()
    v = vault.current_vault.get()
    snap = v.snapshot()
    assert snap.read_bytes().startswith(vault.MAGIC) and b"SQLite format" not in snap.read_bytes()
    assert v.open_snapshot(snap).execute("select count(*) from accounts").fetchone()[0] == 1
    with pytest.raises(vault.VaultError):
        vault.Vault("other", b"y" * 32).open_snapshot(snap)


# ── sealed files ──────────────────────────────────────────────────────────

def test_files_are_sealed_named_opaquely_and_listed_by_prefix():
    v = vault.current_vault.get()
    v.put("inbox/abc.pdf", b"%PDF-secret statement")
    v.put("inbox/def.pdf", b"another")
    v.put("mailboxes.json", b"{}")
    assert v.get("inbox/abc.pdf") == b"%PDF-secret statement" and v.get("missing") is None
    assert v.names("inbox/") == ["inbox/abc.pdf", "inbox/def.pdf"]
    on_disk = list((v.root / "f").iterdir())
    assert len(on_disk) == 3 and all(p.name.endswith(".enc") and "inbox" not in p.name for p in on_disk)
    assert b"secret statement" not in b"".join(p.read_bytes() for p in on_disk)
    v.delete("inbox/abc.pdf")
    assert v.get("inbox/abc.pdf") is None and v.names("inbox/") == ["inbox/def.pdf"]


def test_a_file_cannot_be_moved_to_a_different_name_to_be_read_as_another(tmp_path):
    v = vault.Vault("x", b"k" * 32, tmp_path / "x")
    v.put("a", b"AAA"); v.put("b", b"BBB")
    pa, pb = v._path("a"), v._path("b")
    pb.write_bytes(pa.read_bytes())                                                 # someone with disk access swaps the files
    with pytest.raises(vault.VaultError):
        v.get("b")                                                                  # b is not given a's content
    assert v.get("a") == b"AAA"


def test_the_registry_forgets_a_profile_after_it_has_been_idle():
    now = [1000.0]
    reg = vault.Registry(idle=60, clock=lambda: now[0])
    reg.unlock("u", b"k" * 32)
    now[0] += 30
    assert reg.get("u")                                                              # use resets the clock
    now[0] += 59
    assert reg.get("u")
    now[0] += 61
    assert reg.get("u") is None and not reg.unlocked("u")
    reg.unlock("u", b"k" * 32); reg.lock("u")
    assert reg.get("u") is None
