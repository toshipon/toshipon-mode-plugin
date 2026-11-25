"""Persistent job queue: event-id dedup, leases, bounded retry with backoff, dead letters."""

from datetime import timedelta
from pathlib import Path

from .redact import redact, redact_text
from .store import iso, locked, parse_time, read_json, write_json

TERMINAL = ("done", "dead", "cancelled")


class Queue:
    """All mutations rewrite one small state file atomically under an exclusive lock."""

    def __init__(self, home, settings):
        self.path = Path(home) / "queue" / "jobs.json"
        self.lock = Path(home) / "queue" / "jobs.lock"
        self.settings = settings

    def _mutate(self, change):
        with locked(self.lock):
            jobs = read_json(self.path, {})
            result = change(jobs)
            write_json(self.path, jobs)
            return result

    def jobs(self):
        return read_json(self.path, {})

    def enqueue(self, job_id, kind, payload, now):
        """False when the id was already seen: the same event never queues twice."""

        def change(jobs):
            if job_id in jobs:
                return False
            jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "payload": payload,
                "status": "pending",
                "attempts": 0,
                "not_before": iso(now),
                "created_at": iso(now),
                "updated_at": iso(now),
                "last_error": None,
            }
            return True

        return self._mutate(change)

    def _retry_or_bury(self, job, now, error):
        job["last_error"] = redact_text(str(error))[:500]
        job["updated_at"] = iso(now)
        job.pop("lease_expires_at", None)
        if job.pop("cancel_requested", False):
            job["status"] = "cancelled"
        elif job["attempts"] >= self.settings["max_attempts"]:
            job["status"] = "dead"
        else:
            delay = min(
                self.settings["backoff_base_seconds"] * 2 ** (job["attempts"] - 1),
                self.settings["backoff_max_seconds"],
            )
            job["status"] = "pending"
            job["not_before"] = iso(now + timedelta(seconds=delay))
        return job["status"]

    def claim(self, now, skip_kinds=()):
        """Lease the oldest due job. A lease that ran out (crash, timeout) counts as a failed attempt."""

        def change(jobs):
            for job in jobs.values():
                if job["status"] == "running" and parse_time(job["lease_expires_at"]) <= now:
                    self._retry_or_bury(job, now, "lease expired; the worker crashed or timed out")
            due = [
                job
                for job in jobs.values()
                if job["status"] == "pending"
                and parse_time(job["not_before"]) <= now
                and job["kind"] not in skip_kinds
            ]
            if not due:
                return None
            job = min(due, key=lambda item: (item["created_at"], item["id"]))
            job["status"] = "running"
            job["attempts"] += 1
            job["updated_at"] = iso(now)
            job["lease_expires_at"] = iso(now + timedelta(seconds=self.settings["lease_seconds"]))
            return dict(job)

        return self._mutate(change)

    def complete(self, job_id, now, result):
        def change(jobs):
            job = jobs[job_id]
            job["status"] = "cancelled" if job.pop("cancel_requested", False) else "done"
            job["result"] = redact(result)
            job["updated_at"] = iso(now)
            job.pop("lease_expires_at", None)
            return job["status"]

        return self._mutate(change)

    def fail(self, job_id, now, error):
        return self._mutate(lambda jobs: self._retry_or_bury(jobs[job_id], now, error))

    def release(self, job_id, now, note):
        """Give a claimed job back without spending an attempt (budget stop, not a failure)."""

        def change(jobs):
            job = jobs[job_id]
            job["status"] = "cancelled" if job.pop("cancel_requested", False) else "pending"
            job["attempts"] -= 1
            job["last_error"] = note
            job["updated_at"] = iso(now)
            job.pop("lease_expires_at", None)

        return self._mutate(change)

    def cancel(self, job_id, now):
        """Pending jobs are cancelled at once; a running job is cancelled when its worker returns."""

        def change(jobs):
            job = jobs.get(job_id)
            if not job or job["status"] in TERMINAL:
                return False
            if job["status"] == "running":
                job["cancel_requested"] = True
            else:
                job["status"] = "cancelled"
            job["updated_at"] = iso(now)
            return True

        return self._mutate(change)

    def cancel_requested(self, job_id):
        return bool(self.jobs().get(job_id, {}).get("cancel_requested"))

    def retry(self, job_id, now):
        """Move a dead job back to pending with a fresh attempt budget."""

        def change(jobs):
            job = jobs.get(job_id)
            if not job or job["status"] != "dead":
                return False
            job.update(status="pending", attempts=0, not_before=iso(now), updated_at=iso(now))
            return True

        return self._mutate(change)

    def purge(self, before):
        """Drop terminal jobs last touched before the cutoff. Their ids can queue again afterwards."""

        def change(jobs):
            old = [
                job_id
                for job_id, job in jobs.items()
                if job["status"] in TERMINAL and parse_time(job["updated_at"]) < before
            ]
            for job_id in old:
                del jobs[job_id]
            return len(old)

        return self._mutate(change)
