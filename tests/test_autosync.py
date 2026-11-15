"""Automatic mail sync: per mailbox, every 1 or 6 hours, only while the person's data is unlocked."""

import threading
import time

import pytest

from finance import autosync, secrets, vault
from finance.mailconfig import MailConfig, MailboxAccount, SenderRule, load_config, save_config
from tests.conftest import PASSWORD
from tests.test_web import authed, client  # noqa: F401  (the signed-in web client)


class FakeJobs:
    """Jobs finish instantly unless `hold` is set, which keeps each started job 'running' until the test clears `busy`."""
    def __init__(self): self.started, self.busy, self.hold = [], False, False
    def running(self): return "job-x" if self.busy else None
    def start(self, action, params=None):
        self.started.append((vault.current_vault.get().uid, action, tuple(params["accounts"])))
        self.busy = self.hold
        return f"job-{len(self.started)}"


def mailbox(name="m", hours=6, password=True):
    save_config(MailConfig(accounts=[MailboxAccount(name=name, provider="gmail", username=f"{name}@example.invalid",
                                                    auto_sync_hours=hours, senders=[SenderRule(match="camsonline.com")])]))
    if password:
        secrets.set_secret(f"imap:{name}", "app-pw")


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def auto(registry, me, clock):
    """A scheduler over a registry holding the signed-in test profile, with a clock the test controls."""
    reg = vault.Registry(idle=30 * 24 * 3600, clock=lambda: clock[0])         # long, so schedules are tested apart from the idle lock
    reg.unlock(me["id"], vault.current_vault.get()._dek)
    a = autosync.AutoSync(reg, FakeJobs(), clock=lambda: clock[0])
    a.reg = reg
    return a


@pytest.mark.parametrize("hours", [1, 6])
def test_a_mailbox_syncs_when_enabled_and_then_once_per_interval(auto, clock, me, hours):
    mailbox(hours=hours)
    assert auto.tick() == [(me["id"], "m")]                                   # first time: right away
    assert auto.tick() == []                                                  # not again the same minute
    clock[0] += hours * 3600 - 1
    assert auto.tick() == []                                                  # one second early
    clock[0] += 1
    assert auto.tick() == [(me["id"], "m")]                                   # on the dot
    assert [j[2] for j in auto.jobs.started] == [("m",), ("m",)] and all(j[1] == "sync" for j in auto.jobs.started)


def test_off_means_off(auto, clock):
    mailbox(hours=0)
    clock[0] += 10 * 24 * 3600
    assert auto.tick() == [] and auto.jobs.started == []


def test_only_the_listed_choices_exist():
    assert autosync.AUTO_SYNC_CHOICES == (0, 1, 6)
    for bad in (2, 5, 12, 24, 48, -1):
        with pytest.raises(ValueError):
            MailboxAccount(name="m", provider="gmail", username="u", auto_sync_hours=bad)
    assert load_config().accounts == []                                       # an existing config without the field still loads (default off)


def test_a_mailbox_with_no_app_password_is_skipped(auto):
    mailbox(password=False)
    assert auto.tick() == []


def test_a_running_job_postpones_the_run_without_losing_it(auto, clock, me):
    mailbox(hours=1)
    auto.jobs.busy = True
    assert auto.tick() == [] and autosync.load_state() == {}                  # nothing recorded, so it is still due
    auto.jobs.busy = False
    assert auto.tick() == [(me["id"], "m")]


def test_each_mailbox_has_its_own_schedule(auto, clock, me):
    save_config(MailConfig(accounts=[
        MailboxAccount(name="a", provider="gmail", username="a@example.invalid", auto_sync_hours=1, senders=[SenderRule(match="x.invalid")]),
        MailboxAccount(name="b", provider="gmail", username="b@example.invalid", auto_sync_hours=6, senders=[SenderRule(match="x.invalid")])]))
    secrets.set_secret("imap:a", "p"); secrets.set_secret("imap:b", "p")
    auto.jobs.hold = True
    assert auto.tick() == [(me["id"], "a")]                                  # one job at a time: b follows on the next tick
    auto.jobs.busy = False
    assert auto.tick() == [(me["id"], "b")]
    auto.jobs.busy = False
    clock[0] += 3600
    assert auto.tick() == [(me["id"], "a")]                                  # a is hourly, b is not due for six hours


def test_nothing_runs_while_the_data_is_locked_and_it_resumes_after_unlock_without_catching_up(auto, clock, me):
    mailbox(hours=6)
    auto.tick(); auto.jobs.started.clear()
    auto.reg.lock(me["id"])
    clock[0] += 5 * 24 * 3600                                                 # days pass, locked
    assert auto.tick() == [] and auto.jobs.started == []
    auto.reg.unlock(me["id"], vault.current_vault.get()._dek)
    assert auto.tick() == [(me["id"], "m")]                                   # one run, not twenty missed ones
    assert auto.tick() == []


def test_background_runs_do_not_postpone_the_idle_lock(auto, clock, me):
    auto.reg.idle = 8 * 3600
    mailbox(hours=1)
    runs = []
    for _ in range(12):                                                       # twelve hourly ticks with nobody using the app
        runs.append(len(auto.tick()))
        clock[0] += 3600
    assert runs == [1] * 9 + [0] * 3                                          # runs at hours 0-8; at hour 9 the idle lock has fired, for good
    assert auto.reg.get(me["id"]) is None


def test_using_the_app_does_postpone_the_idle_lock(auto, clock, me):
    auto.reg.idle = 8 * 3600
    mailbox(hours=1)
    for _ in range(9):
        clock[0] += 3600
        assert auto.reg.get(me["id"]) is not None                             # a person's request counts as use
        auto.tick()


def test_each_profile_runs_in_its_own_context_and_keeps_its_own_state(auto, clock, me, users):
    mailbox(hours=1)
    other, dek, _ = users.sign_up("Other Person", "another long password", users.make_invite())
    auto.reg.unlock(other["id"], dek)
    token = vault.current_vault.set(auto.reg.get(other["id"]))
    try:
        mailbox(name="theirs", hours=6)
    finally:
        vault.current_vault.reset(token)
    assert sorted(auto.tick()) == sorted([(me["id"], "m"), (other["id"], "theirs")])
    assert autosync.load_state().keys() == {"m"}                              # my context only sees my schedule


def test_a_removed_mailbox_is_forgotten_and_the_schedule_is_sealed_on_disk(auto, clock):
    mailbox(name="gmail-private-name", hours=1)
    auto.tick()
    on_disk = b"".join(p.read_bytes() for p in vault.current_vault.get().root.rglob("*") if p.is_file())
    assert b"gmail-private-name" not in on_disk
    save_config(MailConfig())
    auto.tick()
    assert autosync.load_state() == {}


def test_the_scheduler_thread_starts_once_and_survives_a_bad_tick(auto, monkeypatch):
    monkeypatch.setattr(autosync, "TICK_SECONDS", 0.01)
    calls = []
    monkeypatch.setattr(auto, "tick", lambda: (calls.append(1), (_ for _ in ()).throw(RuntimeError("boom")))[1])
    auto.start(); first = auto._thread; auto.start()
    time.sleep(0.15)
    assert auto._thread is first and first.is_alive() and first.daemon and len(calls) >= 3


def test_next_run_wording():
    assert autosync.next_run(0, None, 100) is None
    assert autosync.next_run(6, None, 100) == 100                              # never run: due now
    assert autosync.next_run(6, 100, 200) == 100 + 6 * 3600
    assert autosync.next_run(1, 0.5, 10 ** 6) == 10 ** 6                       # overdue: due now


# ── the Mailboxes page ───────────────────────────────────────────────────────────

FORM = {"name": "m", "provider": "gmail", "username": "me@example.invalid", "host": "", "folder": "INBOX", "lookback_days": "365",
        "senders": "camsonline.com | cas", "app_password": "app-pw-123", "require_auth": "1"}


def test_the_form_offers_exactly_off_1_6_12_24_hours(client):
    authed(client)
    page = client.get("/mailboxes").text
    for label in ("Off: only when I press Sync now", "Every hour", "Every 6 hours"):
        assert label in page
    assert 'name="auto_sync_hours"' in page and "does not postpone the idle lock" in page
    assert "Every 12 hours" not in page and "Every 24 hours" not in page and "Every 2 hours" not in page


@pytest.mark.parametrize("hours, shown", [(1, "every 1 hour"), (6, "every 6 hours")])
def test_a_saved_schedule_is_stored_shown_and_editable(client, hours, shown):
    authed(client)
    assert client.post("/mailboxes/save", data={**FORM, "auto_sync_hours": str(hours)}).status_code == 303
    assert load_config().account("m").auto_sync_hours == hours
    page = client.get("/mailboxes").text
    assert shown in page and "Automatic sync:" in page
    assert f'<option value="{hours}" selected>' in client.get("/mailboxes?edit=m").text


@pytest.mark.parametrize("bad", ["5", "12", "24"])
def test_an_unsupported_interval_is_refused_and_nothing_is_saved(client, bad):
    authed(client)
    r = client.post("/mailboxes/save", data={**FORM, "auto_sync_hours": bad})
    assert r.status_code == 200 and "auto_sync_hours" in r.text and load_config().accounts == []


def test_a_mailbox_with_no_choice_made_stays_off(client):
    authed(client)
    client.post("/mailboxes/save", data=FORM)
    assert load_config().account("m").auto_sync_hours == 0 and "Automatic sync: off" in client.get("/mailboxes").text


def test_a_schedule_longer_than_the_idle_lock_carries_a_plain_warning(client):
    authed(client)
    client.post("/mailboxes/save", data={**FORM, "auto_sync_hours": "6"})
    client.registry.idle = 4 * 3600                                           # a server that locks sooner than the schedule
    page = client.get("/mailboxes").text
    assert "locks your data after 4 hours" in page and "KOSH_IDLE_LOCK_HOURS</code> to at least 6" in page
    client.registry.idle = 8 * 3600                                           # the default fits both choices
    assert "locks your data after" not in client.get("/mailboxes").text.split("<h2>")[0]
    client.post("/mailboxes/save", data={**FORM, "auto_sync_hours": "1"})
    client.registry.idle = 3600 // 2
    assert "locks your data after" in client.get("/mailboxes").text           # even hourly cannot run if the lock is under an hour
