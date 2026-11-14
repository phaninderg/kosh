"""Fixes from the second security review: throttling that cannot be raced or used to lock people out, strict redirect targets,
closed vaults, request limits, and a mailbox password that never follows a changed server."""

import asyncio
import threading
import time

import httpx
import pytest

import finance.web.app as webapp
from finance import vault
from finance.web import accounts, auth
from tests.conftest import PASSWORD
from tests.test_accounts import Site


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path)


# ── throttling ───────────────────────────────────────────────────────────────

def test_an_attempt_is_counted_before_the_slow_check_and_forgiven_on_success():
    t = auth.Throttle(clock=lambda: 1000.0)
    assert [t.reserve("a") for _ in range(5)] == [0, 0, 0, 0, 0]
    assert t.reserve("a") > 0
    t.succeeded("a")
    assert t.reserve("a") == 0


def test_there_is_no_global_lock_so_a_stranger_cannot_block_everyone():
    t = auth.Throttle()
    for i in range(500):
        t.reserve(f"client-{i % 3}")                                  # three noisy clients
    assert t.reserve("someone-else") == 0 and t.reserve("uid:abc") == 0


def test_a_burst_of_parallel_wrong_guesses_gets_only_five_tries(site):
    site.person("Asha")

    async def burst():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=site.app), base_url="http://testserver",
                                     headers={"origin": "http://testserver"}) as c:
            return await asyncio.gather(*[c.post("/login", data={"name": "Asha", "password": f"wrong guess number {i}"}) for i in range(40)])
    codes = [r.status_code for r in asyncio.run(burst())]
    assert codes.count(401) == 5 and codes.count(429) == 35


def test_strangers_failing_at_login_cannot_block_a_signed_in_person_from_unlocking(site):
    b, _ = site.person("Asha")
    stranger = site.browser()
    for _ in range(40):
        stranger.post("/login", data={"name": "Asha", "password": "nope nope nope nope"})
    assert stranger.post("/login", data={"name": "Asha", "password": PASSWORD}).status_code == 429       # the stranger is held
    site.registry.lock(site.users.by_name("Asha")["id"])
    assert b.post("/unlock", data={"password": PASSWORD}).status_code == 303                            # the owner is not
    assert b.post("/account/password", data={"current": PASSWORD, "new": "another long passphrase", "confirm": "another long passphrase"}).status_code == 303


# ── redirect targets ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target", ["/\t/evil.example", "/\n/evil.example", "/ /evil.example", "/\r/evil.example", "//evil.example",
                                    "/\\evil.example", "/x\x00y", "/é", "javascript:alert(1)", "https://evil.example"])
def test_a_redirect_target_with_control_characters_or_another_host_becomes_the_home_page(target):
    assert auth.safe_next(target) == "/"


def test_ordinary_targets_survive():
    assert auth.safe_next("/transactions?q=sip&sort=date_desc") == "/transactions?q=sip&sort=date_desc"
    assert auth.safe_next("/unlock?next=%2Faccounts") == "/unlock?next=%2Faccounts"


def test_the_login_page_never_echoes_a_hostile_target_into_the_page(site):
    site.person("Asha")
    page = site.browser().get("/login?next=/%09/evil.example").text
    assert "evil.example" not in page


# ── request limits and busy ledgers ────────────────────────────────────────────────────

def test_oversized_bodies_are_refused_before_they_are_read(site):
    b, _ = site.person("Asha")
    anon = site.browser()
    assert anon.post("/login", content=b"x" * (webapp.MAX_FORM_BYTES + 1), headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413
    assert anon.post("/auth/passkey/login/verify", content=b"{" + b" " * webapp.MAX_FORM_BYTES + b"}", headers={"content-type": "application/json"}).status_code == 413
    assert b.post("/import", content=b"x" * 1000, headers={"content-type": "multipart/form-data; boundary=x"}).status_code in (400, 422, 303, 200)


def test_a_post_without_a_content_length_is_refused(site):
    class NoLength(httpx.Auth):
        def auth_flow(self, request):
            request.headers.pop("content-length", None)
            yield request
    b = site.browser()
    assert b.post("/login", data={"name": "x"}, auth=NoLength()).status_code == 411


def test_a_busy_ledger_answers_503_quickly_instead_of_hanging_a_worker(site, monkeypatch):
    b, _ = site.person("Asha")
    monkeypatch.setattr(vault.Vault, "WRITE_WAIT", 0.05)
    with site.as_user("Asha") as _:
        holder = vault.current_vault.get().connect(writable=True)               # a running sync holds the ledger
        started = time.time()
        r = b.post("/review/scan-duplicates")
        holder.close()
    assert r.status_code == 503 and "busy" in r.text and time.time() - started < 2


def test_one_profile_cannot_use_more_than_its_share_of_workers(site, monkeypatch):
    b, _ = site.person("Asha")
    monkeypatch.setattr(webapp, "MAX_IN_FLIGHT", 0)                         # the per-profile cap, set so low that nothing fits
    r = b.get("/")
    assert r.status_code == 429 and "too many requests" in r.text
    other, _ = site.person("Ravi")
    assert other.get("/").status_code == 429                                  # each profile has its own allowance (also 0 here)
    monkeypatch.setattr(webapp, "MAX_IN_FLIGHT", 6)
    assert site.browser().get("/login").status_code in (200, 303)             # pages anyone may open are not counted


# ── a locked vault is really closed ───────────────────────────────────────────────────────

def test_locking_closes_the_vault_so_a_running_job_cannot_keep_writing(site):
    site.person("Asha")
    uid = site.users.by_name("Asha")["id"]
    old = site.registry.get(uid)
    conn = old.connect(writable=True)
    conn.execute("INSERT INTO accounts (type, institution, name, mask, ref_hash) VALUES ('bank','B','B …1','1','h')")
    site.registry.lock(uid)
    with pytest.raises(vault.VaultLocked):
        conn.commit()                                                              # its commit is refused
    with pytest.raises(vault.VaultLocked):
        old.put("x", b"y")
    with pytest.raises(vault.VaultLocked):
        old.get("x")
    with pytest.raises(vault.VaultLocked):
        old.connect(writable=False)
    assert old._dek is None and old._master is None                              # the key and the cached ledger are forgotten


def test_a_second_unlock_cannot_run_a_second_writer_beside_a_job_from_the_first(site, monkeypatch):
    site.person("Asha")
    uid = site.users.by_name("Asha")["id"]
    first = site.registry.get(uid)
    held = first.connect(writable=True)                                            # a job holds the ledger
    dek = first._dek
    site.registry.lock(uid)
    second = site.registry.unlock(uid, dek)
    monkeypatch.setattr(vault.Vault, "WRITE_WAIT", 0.05)
    assert second is not first and second._write_lock is first._write_lock
    with pytest.raises(vault.VaultBusy):
        second.connect(writable=True)                                              # it waits for the old writer, not alongside it
    held.close()
    second.connect(writable=True).close()


def test_removing_a_profile_stops_its_job_from_recreating_its_folder(site):
    admin, _ = site.person("Asha")
    site.person("Ravi")
    rid = site.users.by_name("Ravi")["id"]
    ravi_vault = site.registry.get(rid)
    assert admin.post(f"/admin/users/{rid}/delete", data={"confirm": "Ravi"}).status_code == 303
    assert not (vault.USERS_DIR / rid).exists()
    with pytest.raises(vault.VaultLocked):
        ravi_vault.put("jobs/late", b"x")                                          # the job's final save
    assert not (vault.USERS_DIR / rid).exists()


def test_temporary_files_are_never_shared_between_writers(tmp_path):
    target = tmp_path / "f.enc"
    seen = set()
    real = vault.os.replace
    def spy(src, dst):
        seen.add(str(src)); return real(src, dst)
    vault.os.replace = spy
    try:
        threads = [threading.Thread(target=vault._atomic_write, args=(target, bytes([i]) * 100)) for i in range(8)]
        [t.start() for t in threads]; [t.join() for t in threads]
    finally:
        vault.os.replace = real
    assert len(seen) == 8 and not list(tmp_path.glob("*.tmp")) and len(target.read_bytes()) == 100


# ── passkey challenges and device codes ──────────────────────────────────────────────────────

def test_one_client_flooding_challenges_only_evicts_its_own(site):
    c = accounts.Challenges()
    victim = c.put("login", b"v", client="victim")
    for _ in range(300):
        c.put("login", b"x", client="attacker")
    assert c.take(victim, "login") is not None
    assert sum(1 for i in c._items.values() if i["client"] == "attacker") <= accounts.MAX_PER_CLIENT


def test_expired_challenges_are_dropped_before_anything_is_evicted():
    now = [0.0]
    c = accounts.Challenges(clock=lambda: now[0])
    old = [c.put("login", b"x", client="a") for _ in range(5)]
    now[0] += accounts.CHALLENGE_TTL + 1
    c.put("login", b"y", client="b")
    assert all(i not in c._items for i in old) and len(c._items) == 1


def test_a_device_code_starts_exactly_one_registration(site):
    from tests.softkey import SoftKey
    site.person("Asha")
    code = site.users.make_device_code(site.users.by_name("Asha")["id"])
    phone = site.browser()
    first = phone.post("/auth/link/options", json={"code": code})
    assert first.status_code == 200
    assert phone.post("/auth/link/options", json={"code": code}).status_code == 400            # claimed: no second challenge
    key = SoftKey()
    ok = phone.post("/auth/link/verify", json={"cid": first.json()["cid"], "name": "p", "credential": key.create(first.json()["options"])})
    assert ok.status_code == 200
    again = phone.post("/auth/link/verify", json={"cid": first.json()["cid"], "name": "p", "credential": SoftKey().create(first.json()["options"])})
    assert again.status_code == 400                                                             # the challenge is spent too


# ── mailbox settings ────────────────────────────────────────────────────────────────────────

def _save(b, **over):
    form = {"name": "m", "provider": "gmail", "username": "me@example.invalid", "host": "", "folder": "INBOX", "lookback_days": "365",
            "senders": "camsonline.com | cas", "app_password": "", "require_auth": "1", **over}
    return b.post("/mailboxes/save", data=form)


def test_a_saved_app_password_does_not_follow_a_changed_server_or_username(site):
    from finance import secrets
    b, _ = site.person("Asha")
    assert _save(b, app_password="app-pw-123").status_code == 303
    with site.as_user("Asha"):
        assert secrets.get_secret("imap:m") == "app-pw-123"
    r = _save(b, provider="custom", host="attacker.example")
    assert "enter the app password again" in r.text
    with site.as_user("Asha"):
        assert secrets.get_secret("imap:m") is None                                              # nothing left to send anywhere
    assert _save(b, app_password="app-pw-123").status_code == 303
    assert "enter the app password again" in _save(b, username="other@example.invalid").text
    assert _save(b, app_password="new-pw", username="other@example.invalid").status_code == 303


def test_a_host_typed_for_gmail_or_yahoo_is_ignored():
    from finance.mailconfig import MailboxAccount
    assert MailboxAccount(name="m", provider="gmail", username="u", host="attacker.example").imap_host == "imap.gmail.com"
    assert MailboxAccount(name="m", provider="yahoo", username="u", host="attacker.example").imap_host == "imap.mail.yahoo.com"
    assert MailboxAccount(name="m", provider="custom", username="u", host="mail.mine.example").imap_host == "mail.mine.example"
