import keyring

from finance import secrets
from tests.conftest import MemKeyring


def test_keyring_then_env_fallback(monkeypatch):
    kr = MemKeyring(); keyring.set_keyring(kr)
    monkeypatch.delenv("KOSH_ID_PAN", raising=False)
    monkeypatch.delenv("PF_ID_PAN", raising=False)
    assert secrets.get_secret("id:pan") is None and not secrets.has_secret("id:pan")
    monkeypatch.setenv("KOSH_ID_PAN", "ENVPAN")
    assert secrets.get_secret("id:pan") == "ENVPAN"
    secrets.set_secret("id:pan", "KEYRINGPAN")
    assert secrets.get_secret("id:pan") == "KEYRINGPAN"          # keyring wins
    secrets.delete_secret("id:pan")
    assert secrets.get_secret("id:pan") == "ENVPAN"


def test_the_old_cas_password_name_and_env_still_fill_the_default_password(monkeypatch):
    keyring.set_keyring(MemKeyring())
    monkeypatch.delenv("KOSH_ID_CAS_PASSWORD", raising=False)
    monkeypatch.delenv("PF_ID_CAS_PASSWORD", raising=False)
    monkeypatch.setenv("CAS_PASSWORD", "legacy")
    assert secrets.get_secret("id:password") == "legacy"
    monkeypatch.delenv("CAS_PASSWORD")
    secrets.set_secret("id:cas_password", "stored-under-the-old-name")
    assert secrets.get_secret("id:password") == "stored-under-the-old-name"


def test_entries_saved_by_the_predecessor_are_still_read_but_new_ones_win(monkeypatch):
    kr = MemKeyring(); keyring.set_keyring(kr)
    for k in ("KOSH_IMAP_ME", "PF_IMAP_ME"):
        monkeypatch.delenv(k, raising=False)
    kr.set_password(secrets.LEGACY_SERVICE, "imap:me", "old-app-password")           # saved before the rename
    assert secrets.get_secret("imap:me") == "old-app-password"
    secrets.set_secret("imap:me", "new-app-password")                                # saved by this app
    assert secrets.get_secret("imap:me") == "new-app-password"
    assert kr.get_password(secrets.LEGACY_SERVICE, "imap:me") == "old-app-password"  # the old entry is never rewritten
    assert kr.get_password(secrets.SERVICE, "imap:me") == "new-app-password"


def test_deleting_a_secret_removes_it_from_the_old_entry_too(monkeypatch):
    kr = MemKeyring(); keyring.set_keyring(kr)
    monkeypatch.delenv("KOSH_ID_EXTRA_PASSWORDS", raising=False)
    monkeypatch.delenv("PF_ID_EXTRA_PASSWORDS", raising=False)
    kr.set_password(secrets.LEGACY_SERVICE, "id:extra_passwords", '["x"]')
    secrets.delete_secret("id:extra_passwords")
    assert secrets.get_secret("id:extra_passwords") is None


def test_the_old_environment_prefix_is_still_read(monkeypatch):
    keyring.set_keyring(MemKeyring())
    monkeypatch.delenv("KOSH_ID_MOBILE", raising=False)
    monkeypatch.setenv("PF_ID_MOBILE", "9999999999")
    assert secrets.get_secret("id:mobile") == "9999999999"
    monkeypatch.setenv("KOSH_ID_MOBILE", "8888888888")
    assert secrets.get_secret("id:mobile") == "8888888888"                           # the new prefix wins


def test_file_store_for_containers_keeps_secrets_in_a_private_file(monkeypatch, tmp_path):
    import stat
    path = tmp_path / "secrets.json"
    monkeypatch.setenv("KOSH_SECRET_STORE", "file")
    monkeypatch.setattr(secrets, "_FileStore", lambda _p, real=secrets._FileStore: real(path))
    secrets.set_secret("imap:work", "app-pw")
    assert secrets.get_secret("imap:work") == "app-pw" and secrets.has_secret("imap:work")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    secrets.delete_secret("imap:work")
    assert secrets.get_secret("imap:work") is None
