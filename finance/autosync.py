"""Automatic mail sync: each mailbox can be set to sync every 1 or 6 hours.

It works only while a person's data is *unlocked* on the server. It never unlocks anything, and it never extends the idle
lock: the idle timer is refreshed only by the person using the app, so an unattended sync cannot keep the data key in memory
for longer than the person chose (KOSH_IDLE_LOCK_HOURS, 8 by default). When the data is locked, the schedule simply waits and
resumes after the next unlock; a run that was missed is not repeated, the next one is just one interval after the last attempt.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time

from finance import secrets, vault
from finance.mailconfig import AUTO_SYNC_CHOICES, load_config

STATE_FILE = "autosync.json"          # sealed in the profile's vault: {mailbox name: when its last automatic run started}
TICK_SECONDS = 60


def load_state() -> dict[str, float]:
    raw = vault.require_vault().get(STATE_FILE)
    return json.loads(raw) if raw else {}


def _save_state(state: dict[str, float]) -> None:
    vault.require_vault().put(STATE_FILE, json.dumps(state).encode())


def next_run(hours: int, last: float | None, now: float) -> float | None:
    """When the next automatic run is due (never earlier than now), or None when automatic sync is off."""
    if not hours:
        return None
    return max((last or 0) + hours * 3600, now) if last else now


class AutoSync:
    def __init__(self, registry: vault.Registry, jobs, clock=time.time):
        self.registry, self.jobs, self.clock = registry, jobs, clock
        self._thread: threading.Thread | None = None

    def tick(self) -> list[tuple[str, str]]:
        """Start whatever is due, for every profile whose data is unlocked right now. Returns (profile id, mailbox) pairs started."""
        started = []
        for v in self.registry.peek_all():                 # looking does not count as use: it must not postpone the idle lock
            token = vault.current_vault.set(v)
            try:
                started += [(v.uid, name) for name in self._due_for_this_profile()]
            except vault.VaultError:                       # locked or busy in the middle of a tick: try again next time
                continue
            finally:
                vault.current_vault.reset(token)
        return started

    def _due_for_this_profile(self) -> list[str]:
        now, saved, started = self.clock(), load_state(), []
        accounts = load_config().accounts
        live = {a.name for a in accounts}
        state = {k: t for k, t in saved.items() if k in live}                  # forget mailboxes that were removed
        for a in accounts:
            if a.auto_sync_hours not in AUTO_SYNC_CHOICES or not a.auto_sync_hours:
                continue
            if not secrets.has_secret(a.secret_key):                           # nothing to log in with
                continue
            if a.name in state and now - state[a.name] < a.auto_sync_hours * 3600:
                continue
            if self.jobs.running():                                            # one sync at a time per profile; try next tick
                break
            try:
                self.jobs.start("sync", {"accounts": [a.name]})
            except RuntimeError:
                break
            state[a.name] = now                                                # counted even if it fails: no tight retry loop
            started.append(a.name)
        if state != saved:
            _save_state(state)
        return started

    def start(self) -> None:
        """Run `tick` every minute on a background thread (the server calls this once at start-up)."""
        if self._thread:
            return

        def loop() -> None:
            while True:
                time.sleep(TICK_SECONDS)
                with contextlib.suppress(Exception):                           # a bad tick must never stop the schedule
                    self.tick()
        self._thread = threading.Thread(target=loop, daemon=True, name="kosh-autosync")
        self._thread.start()
