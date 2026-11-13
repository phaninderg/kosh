"""Runs UI-triggered actions (a mail sync) as subprocesses with per-job log files. One job at a time."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from config import JOBS_DIR
from finance.actions import ACTIONS

ROOT = Path(__file__).resolve().parent.parent

# action -> label
ACTION_INFO = {"sync": "Sync mailboxes"}


class JobManager:
    def __init__(self, jobs_dir: str = JOBS_DIR):
        self.dir = Path(jobs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}

    def start(self, action: str, params: dict | None = None) -> str:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        with self._lock:
            if self.running():
                raise RuntimeError("Another job is still running")
            job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            log = open(self.dir / f"{job_id}.log", "wb")
            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "finance.actions", action, json.dumps(params or {})],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            self._procs[job_id] = proc
            self._write_meta(job_id, {"id": job_id, "action": action, "started": time.time(),
                                      "finished": None, "exit_code": None})
            threading.Thread(target=self._wait, args=(job_id, proc, log), daemon=True).start()
            return job_id

    def _wait(self, job_id: str, proc: subprocess.Popen, log) -> None:
        code = proc.wait()
        log.close()
        meta = self.get(job_id) or {}
        meta.update(finished=time.time(), exit_code=code)
        self._write_meta(job_id, meta)

    def _write_meta(self, job_id: str, meta: dict) -> None:
        (self.dir / f"{job_id}.json").write_text(json.dumps(meta))

    def get(self, job_id: str) -> dict | None:
        if not job_id.replace("-", "").isalnum():
            return None
        try:
            meta = json.loads((self.dir / f"{job_id}.json").read_text())
        except (OSError, ValueError):
            return None
        meta["status"] = ("running" if meta["finished"] is None and job_id in self._procs
                          else "interrupted" if meta["finished"] is None
                          else "done" if meta["exit_code"] == 0 else "failed")
        meta["label"] = ACTION_INFO.get(meta["action"], meta["action"])
        return meta

    def log(self, job_id: str, tail: int = 60_000) -> str:
        try:
            data = (self.dir / f"{job_id}.log").read_bytes()[-tail:]
        except OSError:
            return ""
        return data.decode("utf-8", "replace")

    def running(self) -> str | None:
        for jid, proc in self._procs.items():
            if proc.poll() is None:
                return jid
        return None

    def recent(self, n: int = 10) -> list[dict]:
        ids = sorted((p.stem for p in self.dir.glob("*.json")), reverse=True)[:n]
        return [m for m in (self.get(i) for i in ids) if m]
