"""Durable, database-backed background job queue.

* Jobs are rows in the ``jobs`` table, so queued work survives process
  restarts and temporary API outages.
* ``idempotency_key`` (unique) prevents the same logical job being queued twice.
* Workers claim jobs with a conditional UPDATE, so several workers can run
  concurrently against PostgreSQL or SQLite without double-processing.
* Handlers raise :class:`RetryLater` for retryable failures (exponential
  backoff with jitter) and :class:`PermanentJobError` for failures that must
  not be retried.
"""
from __future__ import annotations

import os
import random
import socket
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_sessionmaker, utcnow
from ..logging_setup import get_logger, log_event, scrub_text
from ..models import Job

log = get_logger(__name__)

Handler = Callable[[Session, dict[str, Any]], Any]
HANDLERS: dict[str, Handler] = {}


class RetryLater(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentJobError(Exception):
    pass


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return deco


def enqueue(
    db: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    run_at: datetime | None = None,
    idempotency_key: str | None = None,
    max_attempts: int | None = None,
) -> Job:
    """Queue a job in the caller's transaction. Returns the existing job when the key is taken."""
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing:
            return existing
    job = Job(
        type=job_type,
        payload=payload or {},
        run_at=run_at or utcnow(),
        idempotency_key=idempotency_key,
        max_attempts=max_attempts or get_settings().job_max_attempts,
    )
    try:
        with db.begin_nested():
            db.add(job)
    except IntegrityError:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing
    return job


def backoff_seconds(attempt: int) -> float:
    s = get_settings()
    delay = min(s.job_backoff_max_seconds, s.job_backoff_base_seconds * (2 ** max(attempt - 1, 0)))
    return delay * random.uniform(0.8, 1.2)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def recover_stale_jobs(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    cutoff = now - timedelta(seconds=get_settings().job_lock_timeout_seconds)
    result = db.execute(
        update(Job)
        .where(Job.status == "running", Job.locked_at < cutoff)
        .values(status="queued", locked_by=None, locked_at=None, last_error="recovered after worker timeout")
    )
    db.commit()
    return result.rowcount or 0


def claim_next(db: Session, wid: str, now: datetime | None = None) -> Job | None:
    now = now or utcnow()
    candidates = db.scalars(
        select(Job.id).where(Job.status == "queued", Job.run_at <= now).order_by(Job.run_at, Job.id).limit(10)
    ).all()
    for job_id in candidates:
        result = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "queued")
            .values(status="running", locked_by=wid, locked_at=now, attempts=Job.attempts + 1)
        )
        db.commit()
        if result.rowcount == 1:
            return db.get(Job, job_id)
    return None


def run_job(job_id: int) -> str:
    SessionLocal = get_sessionmaker()
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            return "missing"
        fn = HANDLERS.get(job.type)
        outcome = "succeeded"
        error: str | None = None
        retry_after: float | None = None
        try:
            if fn is None:
                raise PermanentJobError(f"no handler for job type {job.type}")
            fn(db, dict(job.payload or {}))
            db.commit()
        except RetryLater as exc:
            db.rollback()
            outcome, error, retry_after = "retry", str(exc), exc.retry_after
        except PermanentJobError as exc:
            db.rollback()
            outcome, error = "dead", str(exc)
        except Exception as exc:  # noqa: BLE001 - unexpected errors are retried, then dead-lettered
            db.rollback()
            outcome, error = "retry", f"{type(exc).__name__}: {exc}"
            log.exception("job_unexpected_error", extra={"fields": {"job_id": job_id, "job_type": job.type}})

        job = db.get(Job, job_id)
        assert job is not None
        now = utcnow()
        if outcome == "succeeded":
            job.status, job.finished_at, job.last_error = "succeeded", now, None
        elif outcome == "retry" and job.attempts < job.max_attempts:
            delay = retry_after if retry_after is not None else backoff_seconds(job.attempts)
            job.status, job.run_at = "queued", now + timedelta(seconds=delay)
            job.last_error = scrub_text(error or "")[:2000]
        else:
            job.status, job.finished_at = "dead", now
            job.last_error = scrub_text(error or "")[:2000]
            outcome = "dead"
        job.locked_by = job.locked_at = None
        db.commit()
        log_event(log, "job_finished", job_id=job_id, job_type=job.type, outcome=outcome, attempts=job.attempts)
        return outcome


def run_pending(max_jobs: int = 1000, now: datetime | None = None) -> int:
    """Process all currently due jobs synchronously (used by tests and the CLI)."""
    from . import handlers  # noqa: F401 - register handlers

    processed = 0
    SessionLocal = get_sessionmaker()
    wid = worker_id()
    while processed < max_jobs:
        with SessionLocal() as db:
            job = claim_next(db, wid, now=now)
            job_id = job.id if job else None
        if job_id is None:
            break
        run_job(job_id)
        processed += 1
    return processed


def worker_loop(stop_after: float | None = None) -> None:  # pragma: no cover - long-running loop
    from . import handlers  # noqa: F401
    from .scheduler import schedule_periodic_jobs

    settings = get_settings()
    SessionLocal = get_sessionmaker()
    wid = worker_id()
    started = time.time()
    log_event(log, "worker_started", worker=wid)
    while stop_after is None or time.time() - started < stop_after:
        try:
            with SessionLocal() as db:
                schedule_periodic_jobs(db)
                db.commit()
                recover_stale_jobs(db)
                job = claim_next(db, wid)
                job_id = job.id if job else None
            if job_id is not None:
                run_job(job_id)
                continue
        except Exception:  # noqa: BLE001 - keep the worker alive through DB hiccups
            log.exception("worker_loop_error")
        time.sleep(settings.worker_poll_seconds)
