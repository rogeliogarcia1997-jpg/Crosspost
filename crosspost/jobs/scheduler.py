"""Periodic job scheduling.

Each periodic job is enqueued with an idempotency key derived from its time
bucket, so running several workers (or restarting one) never queues the same
periodic run twice.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from .queue import enqueue


def _bucket(now: datetime, minutes: int) -> str:
    minutes = max(minutes, 1)
    epoch_minutes = int((now - datetime(1970, 1, 1)).total_seconds() // 60)
    return str(epoch_minutes // minutes)


def schedule_periodic_jobs(db: Session, now: datetime | None = None) -> None:
    now = now or utcnow()
    s = get_settings()
    enqueue(db, "mls_sync", {"mode": "incremental", "triggered_by": "schedule"},
            idempotency_key=f"mls_sync:{_bucket(now, s.mls_sync_interval_minutes)}")
    enqueue(db, "check_social_accounts", {}, idempotency_key=f"check_accounts:{_bucket(now, 360)}")
    enqueue(db, "scan_scheduled_posts", {}, idempotency_key=f"scan_posts:{_bucket(now, 5)}")
    enqueue(db, "repost_scan", {}, idempotency_key=f"repost_scan:{_bucket(now, 60)}")
    enqueue(db, "cleanup", {}, idempotency_key=f"cleanup:{_bucket(now, 1440)}")
