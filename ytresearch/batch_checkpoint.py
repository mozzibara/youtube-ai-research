"""Phase 5D: crash-safe synthetic stage ledger; NO multimodal/AGY execution.

A run's real multimodal stages have the same labels, but these checkpoints
are deliberately stored in the separate Phase 5B dry-run SQLite database.
Their digest fields are synthetic attestations, NOT hashes of validated
video artifacts, provider evidence, or a production-completion signal.

Each stage checkpoint has an append-only SHA-256 chain. A stage can be
replayed idempotently when the exact same input/output hashes are supplied;
changed hashes fail closed, including after a worker lease expires and is
explicitly retried by an operator.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Dict, Optional

from .batch_queue import (
    BatchQueueError, _connect, _now, _plan,
)
from .multimodal import MULTIMODAL_STAGES


HEX_SHA = re.compile(r"^[0-9a-f]{64}$")
TABLE_NAME = "batch_synthetic_stages"
GENESIS = "GENESIS"


class SyntheticStageInvalid(ValueError):
    pass


class InjectedSyntheticCrash(RuntimeError):
    """Test-only deterministic crash injection; not exposed in CLI."""


def _digest(body: Dict[str, Any]) -> str:
    # Stable canonical serialization rather than SQLite row string order.
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stage_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS batch_synthetic_stages (
          plan_sha TEXT NOT NULL,
          video_id TEXT NOT NULL,
          stage_index INTEGER NOT NULL,
          stage_name TEXT NOT NULL,
          input_sha256 TEXT NOT NULL,
          output_sha256 TEXT NOT NULL,
          first_attempt INTEGER NOT NULL,
          recorded_utc TEXT NOT NULL,
          previous_sha256 TEXT NOT NULL,
          event_sha256 TEXT NOT NULL,
          PRIMARY KEY(plan_sha,video_id,stage_index),
          UNIQUE(plan_sha,video_id,stage_name)
        )
    """)


def _read_verified(db: sqlite3.Connection, plan_sha: str,
                   video_id: str) -> list[Dict[str, Any]]:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE_NAME,),
    ).fetchone()
    if not exists:
        return []
    rows = db.execute(
        "SELECT stage_index,stage_name,input_sha256,output_sha256,"
        "first_attempt,recorded_utc,previous_sha256,event_sha256 "
        "FROM batch_synthetic_stages WHERE plan_sha=? AND video_id=? "
        "ORDER BY stage_index",
        (plan_sha, video_id),
    ).fetchall()
    prev = GENESIS
    events: list[Dict[str, Any]] = []
    for expected_index, row in enumerate(rows):
        event = dict(row)
        if (event["stage_index"] != expected_index or
            expected_index >= len(MULTIMODAL_STAGES) or
            event["stage_name"] != MULTIMODAL_STAGES[expected_index]):
            raise SyntheticStageInvalid("STAGE_SEQUENCE_GAP_OR_REORDERED")
        if not all(
            isinstance(event[key], str) and HEX_SHA.fullmatch(event[key])
            for key in ("input_sha256", "output_sha256")
        ):
            raise SyntheticStageInvalid("STAGE_DIGEST_INVALID")
        try:
            completed = datetime.fromisoformat(event["recorded_utc"])
            if completed.tzinfo is None:
                raise ValueError("Timezone missing")
        except (ValueError, TypeError) as exc:
            raise SyntheticStageInvalid("STAGE_TIMESTAMP_INVALID") from exc
        if type(event["first_attempt"]) is not int or event["first_attempt"] < 1:
            raise SyntheticStageInvalid("STAGE_ATTEMPT_INVALID")
        actual = event.pop("event_sha256")
        if event["previous_sha256"] != prev or not secrets.compare_digest(
            str(actual), _digest(event)
        ):
            raise SyntheticStageInvalid("STAGE_CHECKPOINT_CHAIN_CORRUPTED")
        event["event_sha256"] = actual
        events.append(event)
        prev = actual
    return events


def _verified_job(db: sqlite3.Connection, plan_sha: str,
                  video_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT status,attempts,lease_token,lease_expires_utc "
        "FROM queue_jobs WHERE plan_sha=? AND video_id=?",
        (plan_sha, video_id),
    ).fetchone()
    if row is None:
        raise SyntheticStageInvalid("STAGE_JOB_NOT_FOUND")
    return row


def synthetic_stage_status(root: Path, plan_sha: str,
                           video_id: str) -> Dict[str, Any]:
    """Read/replay the chain without writing; never exposes a lease token."""
    _plan(root, plan_sha, check_stale=False)
    with closing(_connect(root)) as db:
        job = _verified_job(db, plan_sha, video_id)
        events = _read_verified(db, plan_sha, video_id)
    count = len(events)
    return {
        "mode": "SYNTHETIC_DRYRUN_ONLY",
        "plan_sha256": plan_sha,
        "video_id": video_id,
        "job_status": job["status"],
        "stages_done": count,
        "stages_total": len(MULTIMODAL_STAGES),
        "next_stage": (MULTIMODAL_STAGES[count]
                       if count < len(MULTIMODAL_STAGES) else None),
        "synthetic_stage_complete": count == len(MULTIMODAL_STAGES),
        "stages": [
            {
                "stage_name": event["stage_name"],
                "stage_index": event["stage_index"],
                "input_sha256": event["input_sha256"],
                "output_sha256": event["output_sha256"],
                "first_attempt": event["first_attempt"],
                "event_sha256": event["event_sha256"],
            }
            for event in events
        ],
        "production_analysis_completed": False,
        "real_artifact_contents_verified": False,
    }


def assert_synthetic_stage_completion_locked(db: sqlite3.Connection,
                                             plan_sha: str,
                                             video_id: str) -> None:
    """Existing Phase 5B tests with no synthetic stages remain unaffected."""
    rows = _read_verified(db, plan_sha, video_id)
    if rows and len(rows) != len(MULTIMODAL_STAGES):
        raise SyntheticStageInvalid("STAGE_SIMULATED_COMPLETION_INCOMPLETE")


def record_synthetic_stage(
    root: Path, plan_sha: str, video_id: str, *,
    lease_token: str, stage_name: str,
    input_sha256: str, output_sha256: str,
    now_utc: Optional[datetime] = None,
    inject_fault: Optional[str] = None,
) -> Dict[str, Any]:
    """Transactionally record exactly one simulated stage or return replay.

    Test-only fault locations:
      before_insert: nothing persisted
      after_insert: INSERT is rolled back
      after_commit: checkpoint persisted but caller observed a crash

    No production command exposes inject_fault.
    """
    if inject_fault not in (None, "before_insert", "after_insert", "after_commit"):
        raise SyntheticStageInvalid("STAGE_FAULT_POINT_INVALID")
    if stage_name not in MULTIMODAL_STAGES:
        raise SyntheticStageInvalid("STAGE_UNKNOWN_NAME")
    if not all(
        isinstance(sha, str) and HEX_SHA.fullmatch(sha)
        for sha in (input_sha256, output_sha256)
    ):
        raise SyntheticStageInvalid("STAGE_INPUT_OR_OUTPUT_HASH_INVALID")
    _plan(root, plan_sha, check_stale=False)
    now = _now(now_utc)
    replayed = False
    with closing(_connect(root)) as db:
        _stage_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            job = _verified_job(db, plan_sha, video_id)
            if (job["status"] != "LEASED" or
                not isinstance(lease_token, str) or
                not secrets.compare_digest(
                    str(job["lease_token"] or ""), lease_token
                )):
                raise SyntheticStageInvalid("STAGE_ACTIVE_LEASE_REQUIRED")
            expires = datetime.fromisoformat(job["lease_expires_utc"])
            if expires <= now:
                raise SyntheticStageInvalid("STAGE_LEASE_EXPIRED")
            prior = _read_verified(db, plan_sha, video_id)
            index = MULTIMODAL_STAGES.index(stage_name)
            if index < len(prior):
                earlier = prior[index]
                if (earlier["input_sha256"] != input_sha256 or
                    earlier["output_sha256"] != output_sha256):
                    raise SyntheticStageInvalid("STAGE_REPLAY_HASH_MISMATCH")
                replayed = True
            elif index != len(prior):
                raise SyntheticStageInvalid("STAGE_OUT_OF_ORDER")
            else:
                if inject_fault == "before_insert":
                    raise InjectedSyntheticCrash("INJECTED_BEFORE_STAGE_INSERT")
                event = {
                    "stage_index": index,
                    "stage_name": stage_name,
                    "input_sha256": input_sha256,
                    "output_sha256": output_sha256,
                    "first_attempt": int(job["attempts"]),
                    "recorded_utc": now.isoformat(timespec="microseconds"),
                    "previous_sha256": (
                        prior[-1]["event_sha256"] if prior else GENESIS
                    ),
                }
                digest = _digest(event)
                db.execute(
                    "INSERT INTO batch_synthetic_stages "
                    "(plan_sha,video_id,stage_index,stage_name,input_sha256,"
                    "output_sha256,first_attempt,recorded_utc,previous_sha256,"
                    "event_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (plan_sha,video_id,index,stage_name,input_sha256,
                     output_sha256,event["first_attempt"],event["recorded_utc"],
                     event["previous_sha256"],digest),
                )
                if inject_fault == "after_insert":
                    raise InjectedSyntheticCrash("INJECTED_AFTER_STAGE_INSERT")
            db.commit()
        except Exception:
            db.rollback()
            raise
    if inject_fault == "after_commit" and not replayed:
        raise InjectedSyntheticCrash("INJECTED_AFTER_STAGE_COMMIT")
    result = synthetic_stage_status(root, plan_sha, video_id)
    result["replayed_existing_checkpoint"] = replayed
    return result
