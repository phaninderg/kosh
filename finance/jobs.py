"""Long tasks started from the UI (a mail sync), run on a background thread inside the server as the signed-in person.

A job runs with that person's vault, so it can read their mailbox settings and write their ledger, and nobody else can see
it: a job id only resolves for the profile that started it. One job runs per profile at a time. The log is kept in memory
while the job runs and sealed into the profile's vault when it ends."""

from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid

from finance import vault

# action -> label
ACTION_INFO = {"sync": "Sync mailboxes"}
ACTIONS = tuple(ACTION_INFO)
KEEP = 20                                               # finished jobs kept per profile


def run_sync(params: dict, log) -> int:
    from finance.mailconfig import load_config
    from finance.sync import SyncRunner
    cfg = load_config()
    if not cfg.accounts:
        log("No mailboxes configured. Add one on the Mailboxes page first.")
        return 1
    summary = SyncRunner(config=cfg, log=log).run(params.get("accounts") or None)
    log("\nSync summary\n" + summary.render())
    return 1 if any(a.error for a in summary.accounts) else 0


class _Job:
    def __init__(self, uid: str, job_id: str, action: str):
        self.meta = {"id": job_id, "uid": uid, "action": action, "started": time.time(), "finished": None, "exit_code": None}
        self.lines: list[str] = []
        self.lock = threading.Lock()

    def log(self, line: str) -> None:
        with self.lock:
            self.lines.append(str(line))


class JobManager:
    def __init__(self):
        self._lock = threading.RLock()                 # start() asks running() while holding it
        self._jobs: dict[str, _Job] = {}                # running and recently finished, in memory

    # helpers ---------------------------------------------------------------------

    @staticmethod
    def _me() -> vault.Vault:
        return vault.require_vault()

    def _mine(self, job_id: str) -> _Job | None:
        job = self._jobs.get(job_id)
        return job if job and job.meta["uid"] == self._me().uid else None

    def running(self) -> str | None:
        uid = self._me().uid
        with self._lock:
            return next((j.meta["id"] for j in self._jobs.values() if j.meta["uid"] == uid and j.meta["finished"] is None), None)

    # start / wait ----------------------------------------------------------------

    def start(self, action: str, params: dict | None = None) -> str:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        v = self._me()
        with self._lock:
            if self.running():
                raise RuntimeError("Another job is still running")
            job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            job = self._jobs[job_id] = _Job(v.uid, job_id, action)
            ctx = contextvars.copy_context()             # carries this person's vault into the thread
            threading.Thread(target=ctx.run, args=(self._run, job, v, params or {}), daemon=True).start()
            return job_id

    def _run(self, job: _Job, v: vault.Vault, params: dict) -> None:
        try:
            code = run_sync(params, job.log)
        except vault.VaultBusy as exc:
            job.log(str(exc)); code = 1
        except Exception as exc:                        # the log says what happened; the server keeps running
            job.log(f"{type(exc).__name__}: {str(exc)[:300]}"); code = 1
        job.meta.update(finished=time.time(), exit_code=code)
        try:
            v.put(f"jobs/{job.meta['id']}", json.dumps({"meta": job.meta, "log": "\n".join(job.lines)}).encode())
            for old in v.names("jobs/")[:-KEEP]:
                v.delete(old)
        except Exception:
            pass

    # read --------------------------------------------------------------------------

    def get(self, job_id: str) -> dict | None:
        if not job_id.replace("-", "").isalnum():
            return None
        job, meta = self._mine(job_id), None
        if job:
            meta = dict(job.meta)
        else:
            saved = self._me().get(f"jobs/{job_id}")
            meta = json.loads(saved)["meta"] if saved else None
        if not meta:
            return None
        meta["status"] = ("running" if meta["finished"] is None and job else "interrupted" if meta["finished"] is None
                          else "done" if meta["exit_code"] == 0 else "failed")
        meta["label"] = ACTION_INFO.get(meta["action"], meta["action"])
        return meta

    def log(self, job_id: str, tail: int = 60_000) -> str:
        job = self._mine(job_id)
        if job:
            with job.lock:
                return "\n".join(job.lines)[-tail:]
        saved = self._me().get(f"jobs/{job_id}") if job_id.replace("-", "").isalnum() else None
        return json.loads(saved)["log"][-tail:] if saved else ""

    def recent(self, n: int = 10) -> list[dict]:
        ids = {j.meta["id"] for j in self._jobs.values() if j.meta["uid"] == self._me().uid}
        ids |= {name.removeprefix("jobs/") for name in self._me().names("jobs/")}
        return [m for m in (self.get(i) for i in sorted(ids, reverse=True)[:n]) if m]
