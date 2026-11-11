"""Phase 5B: single-worker transactional DRY-RUN lease queue, no AGY.

This deliberately does not start a real model or media process. Queue results
are SIMULATED, never COMPLETED. A live worker must be a separate reviewed PR,
after the pilot quality gate. SQLite BEGIN IMMEDIATE prevents concurrent claims.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Dict, Optional

from .batch_plan import plan_research_batch
from .storage import json_text, read_json
from .batch_budget import (
    authorize_reservation_locked,
    settle_reservation_locked,
    assert_retry_reconciled_locked,
    pause_global_rate_locked, BatchBudgetError,
    _get_policy,
)


MAX_ATTEMPTS = 2
LEASE_MINUTES = 30
PLAN_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class BatchQueueError(ValueError):
    pass


def _now(now_utc: Optional[datetime] = None) -> datetime:
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise BatchQueueError("QUEUE_TIME_REQUIRES_TZ")
    return now.astimezone(timezone.utc)


def _plan(root: Path, digest: str, *, check_stale: bool) -> Dict[str, Any]:
    if not PLAN_PATTERN.fullmatch(digest):
        raise BatchQueueError("QUEUE_PLAN_SHA_INVALID")
    file = Path(root) / "data" / "batch_plans" / f"batch_plan_{digest[:16]}.json"
    if not file.is_file():
        raise BatchQueueError("QUEUE_PLAN_NOT_SAVED")
    plan = read_json(file)
    if not isinstance(plan, dict) or plan.get("plan_sha256") != digest:
        raise BatchQueueError("QUEUE_PLAN_HASH_MISMATCH")
    body = {k: v for k, v in plan.items() if k != "plan_sha256"}
    actual = hashlib.sha256(json_text(body).encode("utf-8")).hexdigest()
    if actual != digest or plan.get("execution_mode") != "PLAN_ONLY_NO_MODEL_OR_NETWORK":
        raise BatchQueueError("QUEUE_PLAN_CONTENT_TAMPERED")
    if plan.get("production_live_batch_authorized") is not False:
        raise BatchQueueError("QUEUE_LIVE_PLAN_FORBIDDEN")
    if check_stale:
        current = plan_research_batch(
            root,
            expected=plan["source_entries"],
            max_jobs=plan["max_jobs"],
            retry_failed=plan["retry_failed_requested"],
            save=False,
        )
        if current["plan_sha256"] != digest:
            raise BatchQueueError("QUEUE_PLAN_STALE_REPLAN_REQUIRED")
    return plan


def _connect(root: Path, *, create: bool = False):
    path = Path(root) / "data" / "batch_dryrun_queue.sqlite3"
    if not create and not path.is_file():
        raise BatchQueueError("QUEUE_NOT_INITIALIZED")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite does not allow multiple concurrent writers; BEGIN IMMEDIATE is
    # used by each queue transition for deterministic one-worker locking.
    connection = sqlite3.connect(path, timeout=20, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    if create:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS queue_plans (
          plan_sha TEXT PRIMARY KEY,
          created_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS queue_jobs (
          plan_sha TEXT NOT NULL REFERENCES queue_plans(plan_sha),
          video_id TEXT NOT NULL,
          job_order INTEGER NOT NULL,
          status TEXT NOT NULL CHECK (status IN
            ('PENDING','LEASED','SIMULATED','PAUSED_FAILURE','PAUSED_EXPIRED')),
          attempts INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT,
          owner TEXT,
          lease_expires_utc TEXT,
          updated_utc TEXT NOT NULL,
          PRIMARY KEY(plan_sha,video_id),
          UNIQUE(plan_sha,job_order)
        );
        """)
    return connection


def initialize_batch_queue(root: Path, plan_sha: str,
                           *, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    plan = _plan(root, plan_sha, check_stale=True)
    selected = plan["plan_only_video_ids"]
    if not selected:
        raise BatchQueueError("QUEUE_NO_PLANNED_JOBS")
    stamp = _now(now_utc).isoformat()
    with closing(_connect(root, create=True)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            # An existing active plan is never overwritten by a new plan.
            active = db.execute(
                "SELECT DISTINCT plan_sha FROM queue_jobs "
                "WHERE status IN ('PENDING','LEASED')"
            ).fetchall()
            if any(row["plan_sha"] != plan_sha for row in active):
                raise BatchQueueError("QUEUE_ANOTHER_ACTIVE_PLAN")
            existing = db.execute(
                "SELECT video_id FROM queue_jobs WHERE plan_sha=? ORDER BY job_order",
                (plan_sha,),
            ).fetchall()
            if existing:
                if [r["video_id"] for r in existing] != selected:
                    raise BatchQueueError("QUEUE_EXISTING_PLAN_DIFFERENT_JOB_SET")
            else:
                db.execute(
                    "INSERT INTO queue_plans(plan_sha,created_utc) VALUES (?,?)",
                    (plan_sha,stamp),
                )
                for order, video_id in enumerate(selected, 1):
                    db.execute(
                        "INSERT INTO queue_jobs "
                        "(plan_sha,video_id,job_order,status,updated_utc) "
                        "VALUES (?,?,?,'PENDING',?)",
                        (plan_sha,video_id,order,stamp),
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return batch_queue_status(root, plan_sha)


def batch_queue_status(root: Path, plan_sha: str) -> Dict[str, Any]:
    _plan(root, plan_sha, check_stale=False)
    with closing(_connect(root)) as db:
        items = [dict(x) for x in db.execute(
            "SELECT video_id,job_order,status,attempts,owner,lease_expires_utc "
            "FROM queue_jobs WHERE plan_sha=? ORDER BY job_order",
            (plan_sha,),
        )]
    if not items:
        raise BatchQueueError("QUEUE_UNKNOWN_PLAN")
    # Do not expose lease tokens in ordinary status output.
    return {
        "mode": "SYNTHETIC_DRYRUN_ONLY",
        "plan_sha256": plan_sha,
        "total_jobs": len(items),
        "counts": dict(sorted(Counter(x["status"] for x in items).items())),
        "jobs": items,
        "live_agy_enabled": False,
    }


def claim_batch_job(root: Path, plan_sha: str, *, owner: str,
                    model: Optional[str] = None,
                    now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    if not owner or len(owner.strip()) > 120:
        raise BatchQueueError("QUEUE_OWNER_INVALID")
    _plan(root, plan_sha, check_stale=True)
    now = _now(now_utc)
    with closing(_connect(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            leased = db.execute(
                "SELECT video_id,lease_expires_utc FROM queue_jobs WHERE status='LEASED'"
            ).fetchall()
            if leased:
                # Expired locks require a deliberate recovery, not reissue.
                raise BatchQueueError("QUEUE_ACTIVE_OR_EXPIRED_LEASE_REQUIRES_RELEASE")
            row = db.execute(
                "SELECT video_id,attempts FROM queue_jobs "
                "WHERE plan_sha=? AND status='PENDING' ORDER BY job_order LIMIT 1",
                (plan_sha,),
            ).fetchone()
            if row is None:
                raise BatchQueueError("QUEUE_NO_PENDING_JOBS")
            if row["attempts"] >= MAX_ATTEMPTS:
                raise BatchQueueError("QUEUE_MAX_ATTEMPTS_EXCEEDED")
            # Budget and global rate checks reserve BEFORE the lease changes,
            # atomically in this very same SQLite writer transaction.
            budget_reservation = authorize_reservation_locked(
                db, plan_sha, row["video_id"], row["attempts"] + 1,
                model, now,
            )
            token = secrets.token_hex(24)
            expires = (now + timedelta(minutes=LEASE_MINUTES)).isoformat()
            db.execute(
                "UPDATE queue_jobs SET status='LEASED',attempts=attempts+1,"
                "lease_token=?,owner=?,lease_expires_utc=?,updated_utc=? "
                "WHERE plan_sha=? AND video_id=? AND status='PENDING'",
                (token, owner.strip(), expires, now.isoformat(),
                 plan_sha, row["video_id"]),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {
        "mode": "SYNTHETIC_DRYRUN_ONLY",
        "plan_sha256": plan_sha, "video_id": row["video_id"],
        "lease_token": token, "lease_expires_utc": expires,
        "budget_reservation": budget_reservation,
        "network_or_model_execution": False,
    }


def finish_batch_job(root: Path, plan_sha: str, *, video_id: str,
                     lease_token: str, success: bool,
                     usage: Optional[Dict[str, Any]] = None,
                     rate_error_code: Optional[str] = None,
                     retry_after_seconds: int = 0,
                     now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    _plan(root, plan_sha, check_stale=False)
    now = _now(now_utc)
    with closing(_connect(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT status,attempts,lease_token,lease_expires_utc FROM queue_jobs "
                "WHERE plan_sha=? AND video_id=?", (plan_sha,video_id),
            ).fetchone()
            if row is None or row["status"] != "LEASED" or not secrets.compare_digest(
                str(row["lease_token"] or ""), str(lease_token or "")
            ):
                raise BatchQueueError("QUEUE_LEASE_TOKEN_OR_STATUS_INVALID")
            if datetime.fromisoformat(row["lease_expires_utc"]) <= now:
                raise BatchQueueError("QUEUE_LEASE_EXPIRED_RECOVERY_REQUIRED")
            if success:
                # If the synthetic stage journal has been started, a job
                # cannot be marked SIMULATED until all nine checkpoints
                # are intact and in order. Legacy no-checkpoint jobs retain
                # the Phase 5B simulation behavior.
                from .batch_checkpoint import assert_synthetic_stage_completion_locked
                assert_synthetic_stage_completion_locked(db, plan_sha, video_id)
                from .batch_worker_contract import assert_verified_stage_completion_if_started_locked
                assert_verified_stage_completion_if_started_locked(db, root, plan_sha, video_id)
            if rate_error_code:
                if success:
                    raise BatchBudgetError("GLOBAL_RATE_ERROR_REQUIRES_FAILED_JOB")
                if _get_policy(db, plan_sha) is None:
                    raise BatchBudgetError("BUDGET_POLICY_REQUIRED_FOR_RATE_EVENT")
                # A failed 429 may have no trustworthy usage record.
                # Keep its worst-case reservation rather than assuming zero.
                budget_accounting = (
                    settle_reservation_locked(
                        db, plan_sha, video_id, row["attempts"], usage, now,
                    ) if usage is not None else None
                )
                pause_global_rate_locked(
                    db, error_code=rate_error_code, actor="dryrun-queue",
                    reason="Synthetic provider rate-limit signal; manual recovery required.",
                    retry_after_seconds=retry_after_seconds, now=now,
                )
            else:
                # Unknown/out-of-reservation usage blocks completion and
                # retains both the lease and worst-case reserved budget.
                budget_accounting = settle_reservation_locked(
                    db, plan_sha, video_id, row["attempts"], usage, now,
                )
            status = "SIMULATED" if success else "PAUSED_FAILURE"
            db.execute(
                "UPDATE queue_jobs SET status=?,lease_token=NULL,"
                "owner=NULL,lease_expires_utc=NULL,updated_utc=? "
                "WHERE plan_sha=? AND video_id=?",
                (status,now.isoformat(),plan_sha,video_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"video_id":video_id,"status":status,
            "budget_accounting": budget_accounting,
            "global_rate_paused": bool(rate_error_code),
            "mode":"SYNTHETIC_DRYRUN_ONLY","live_agy_enabled":False}


def recover_batch_job(root: Path, plan_sha: str, *, video_id: str,
                      actor: str, reason: str, retry: bool = False,
                      now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    if not actor or len(reason.strip()) < 25:
        raise BatchQueueError("QUEUE_RECOVERY_ACTOR_REASON_REQUIRED")
    _plan(root, plan_sha, check_stale=False)
    now = _now(now_utc)
    with closing(_connect(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT status,attempts,lease_expires_utc FROM queue_jobs "
                "WHERE plan_sha=? AND video_id=?",
                (plan_sha,video_id),
            ).fetchone()
            if row is None:
                raise BatchQueueError("QUEUE_JOB_NOT_FOUND")
            if not retry:
                if row["status"] != "LEASED":
                    raise BatchQueueError("QUEUE_EXPIRED_LEASE_REQUIRED")
                if datetime.fromisoformat(row["lease_expires_utc"]) > now:
                    raise BatchQueueError("QUEUE_LEASE_NOT_YET_EXPIRED")
                new_status = "PAUSED_EXPIRED"
            else:
                if row["status"] not in ("PAUSED_FAILURE","PAUSED_EXPIRED"):
                    raise BatchQueueError("QUEUE_RETRY_REQUIRES_PAUSED_JOB")
                assert_retry_reconciled_locked(db, plan_sha, video_id)
                if row["attempts"] >= MAX_ATTEMPTS:
                    raise BatchQueueError("QUEUE_MAX_ATTEMPTS_EXCEEDED")
                new_status = "PENDING"
            # Recovery requires a human action, not an automatic retry.
            db.execute(
                "UPDATE queue_jobs SET status=?,lease_token=NULL,owner=NULL,"
                "lease_expires_utc=NULL,updated_utc=? "
                "WHERE plan_sha=? AND video_id=?",
                (new_status,now.isoformat(),plan_sha,video_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"video_id":video_id, "status":new_status,
            "actor":actor, "reason":reason, "mode":"SYNTHETIC_DRYRUN_ONLY"}
