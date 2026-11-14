"""Secrets live in the signed-in profile's vault, and only there."""

import json

import pytest

from finance import secrets, vault


def test_a_secret_round_trips_and_is_gone_after_delete():
    secrets.set_secret("imap:work", "app-pw")
    assert secrets.get_secret("imap:work") == "app-pw" and secrets.has_secret("imap:work")
    secrets.delete_secret("imap:work")
    assert secrets.get_secret("imap:work") is None and not secrets.has_secret("imap:work")
    secrets.delete_secret("never-set")                                         # deleting what is not there is fine


def test_secrets_are_sealed_on_disk(me):
    secrets.set_secret("id:pan", "ABCDE1234F")
    v = vault.current_vault.get()
    blob = b"".join(p.read_bytes() for p in (v.root / "f").glob("*.enc"))
    assert b"ABCDE1234F" not in blob and b"id:pan" not in blob and blob.startswith(vault.MAGIC)


def test_environment_variables_and_the_old_keyring_names_are_not_consulted(monkeypatch):
    """A variable on the server would be shared by every profile, so nothing is read from the environment."""
    monkeypatch.setenv("KOSH_IMAP_WORK", "from-env")
    monkeypatch.setenv("KOSH_ID_PAN", "from-env")
    monkeypatch.setenv("CAS_PASSWORD", "from-env")
    assert secrets.get_secret("imap:work") is None and secrets.get_secret("id:pan") is None
    assert secrets.get_secret("id:password") is None


def test_outside_a_signed_in_context_nothing_is_readable_and_nothing_can_be_written():
    token = vault.current_vault.set(None)
    try:
        assert secrets.get_secret("id:pan") is None and not secrets.has_secret("id:pan")
        with pytest.raises(vault.VaultLocked):
            secrets.set_secret("id:pan", "x")
        secrets.delete_secret("id:pan")
    finally:
        vault.current_vault.reset(token)


def test_two_profiles_never_see_each_others_secrets(users, registry):
    secrets.set_secret("id:pan", "MINE")
    other, dek, _ = users.sign_up("Other Person", "another long password", users.make_invite())
    token = vault.current_vault.set(registry.unlock(other["id"], dek))
    try:
        assert secrets.get_secret("id:pan") is None
        secrets.set_secret("id:pan", "THEIRS")
        assert secrets.get_secret("id:pan") == "THEIRS"
    finally:
        vault.current_vault.reset(token)
    assert secrets.get_secret("id:pan") == "MINE"
