"""Phase 5F.1: immutable content-hash receipts for synthetic 9-stage outputs.

The actual commit marker is a single SQLite transaction; SHA-256-addressed
files are copied and fsynced before that transaction. Orphan objects from
crashes are never evidence of a committed receipt.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .batch_queue import _connect, _now
from .batch_worker_contract import (
    WorkerContractError,
    _assert_worker_lease_locked,
    _contract_tables,
    _read_verified_stage_chain_locked,
    _verify_completed_run_artifacts_on_disk,
    finish_worker_contract_job,
    streaming_file_sha256,
    validate_worker_input_contract,
    verify_approved_artifact_path,
)
from .multimodal import MULTIMODAL_STAGES
from .storage import json_text

RECEIPT_TABLE = "phase5f1_immutable_receipts"
STORE_NAME = ".phase5f1"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_COUNT = 48
MAX_MANIFEST_BYTES = 32 * 1024


class Phase5FReceiptError(ValueError):
    """Constant error codes; never expose original file content or lease token."""


def _safe_path(root: Path, video_id: str, path: Path) -> Path:
    try:
        return verify_approved_artifact_path(root, video_id, path)
    except WorkerContractError as exc:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_PATH_UNSAFE") from exc


def _safe_directory(root: Path, vid: str, path: Path) -> Path:
    candidate = _safe_path(root, vid, path)
    if candidate.exists() and (candidate.is_symlink() or not candidate.is_dir()):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_STORE_UNSAFE")
    candidate.mkdir(parents=True, exist_ok=True)
    return _safe_path(root, vid, candidate)


def _atomic_content_object(root: Path, video_id: str, store: Path,
                           src: Path | None = None,
                           data: bytes | None = None) -> tuple[str, int]:
    """Write or recheck a SHA-addressed object; never accept a mismatched object."""
    if (src is None) == (data is None):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_SOURCE_INVALID")
    fd, temp_name = tempfile.mkstemp(prefix=".writing-", suffix=".tmp", dir=store)
    temp = Path(temp_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as target:
            if src is not None:
                source = _safe_path(root, video_id, src)
                if not source.is_file():
                    raise Phase5FReceiptError("PHASE5F_RECEIPT_SOURCE_MISSING")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                src_fd = os.open(source, flags)
                with os.fdopen(src_fd, "rb") as incoming:
                    while True:
                        block = incoming.read(65536)
                        if not block:
                            break
                        total += len(block)
                        if total > MAX_ARTIFACT_BYTES:
                            raise Phase5FReceiptError("PHASE5F_RECEIPT_SOURCE_TOO_LARGE")
                        target.write(block)
                        digest.update(block)
            else:
                total = len(data)
                if total > MAX_MANIFEST_BYTES:
                    raise Phase5FReceiptError("PHASE5F_RECEIPT_MANIFEST_TOO_LARGE")
                target.write(data)
                digest.update(data)
            target.flush()
            os.fsync(target.fileno())
        sha = digest.hexdigest()
        target_path = _safe_path(root, video_id, store / sha)
        if target_path.exists():
            if not target_path.is_file():
                raise Phase5FReceiptError("PHASE5F_RECEIPT_OBJECT_INVALID")
            actual_sha, actual_size = streaming_file_sha256(target_path)
            if actual_sha != sha or actual_size != total:
                raise Phase5FReceiptError("PHASE5F_RECEIPT_OBJECT_CORRUPTED")
        else:
            os.replace(temp, target_path)
            if target_path.is_symlink():
                raise Phase5FReceiptError("PHASE5F_RECEIPT_OBJECT_INVALID")
        # Verify published object, not just temporary bytes.
        actual_sha, actual_size = streaming_file_sha256(target_path)
        if actual_sha != sha or actual_size != total:
            raise Phase5FReceiptError("PHASE5F_RECEIPT_OBJECT_CORRUPTED")
        return sha, total
    finally:
        temp.unlink(missing_ok=True)


def _receipt_table(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS " + RECEIPT_TABLE + " ("
        "plan_sha TEXT NOT NULL,video_id TEXT NOT NULL,run_id TEXT NOT NULL,"
        "attempt INTEGER NOT NULL,manifest_sha TEXT NOT NULL,"
        "committed_utc TEXT NOT NULL,"
        "PRIMARY KEY(plan_sha,video_id,run_id),"
        "UNIQUE(plan_sha,video_id,attempt))"
    )


def _run_dir(root: Path, video_id: str, run_id: str) -> Path:
    return _safe_path(
        root, video_id,
        root / "data" / "videos" / video_id / "runs" / run_id
        / "multimodal_checkpoint.json",
    ).parent


def _manifest_payload(contract: Mapping[str, Any],
                      artifacts: list[dict[str, Any]]) -> bytes:
    return json_text({
        "schema_version": 1,
        "plan_sha256": contract["plan_sha256"],
        "video_id": contract["video_id"],
        "run_id": contract["run_id"],
        "attempt": contract["attempt"],
        "artifacts": sorted(artifacts, key=lambda item: item["relpath"]),
    }).encode("utf-8")


def stage_phase5f_receipt(
    root: Path, contract: Mapping[str, Any], *,
    now_utc: datetime,
    fault_after: str | None = None,
) -> dict[str, Any]:
    """Snapshot verified synthetic outputs and atomically commit a receipt.

    Fault points are deterministic test hooks; they do not bypass a lease.
    """
    if fault_after not in (None, "objects", "db_commit"):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_FAULT_INVALID")
    root = Path(root).resolve()
    now = _now(now_utc)
    validate_worker_input_contract(root, contract, now_utc=now, check_stale_plan=False)
    plan_sha = str(contract["plan_sha256"])
    vid = str(contract["video_id"])
    run_id = str(contract["run_id"])

    with closing(_connect(root)) as db:
        events = _read_verified_stage_chain_locked(
            db, root, plan_sha, vid, verify_files_on_disk=True,
        )
    if len(events) != len(MULTIMODAL_STAGES):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_INCOMPLETE_STAGES")
    if any(event["run_id"] != run_id for event in events):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_RUN_MISMATCH")
    _verify_completed_run_artifacts_on_disk(root, vid, run_id, events)

    original = set()
    for event in events:
        for item in event["output_manifest"]["artifacts"]:
            original.add(str(item["relpath"]))
    checkpoint = _run_dir(root, vid, run_id) / "multimodal_checkpoint.json"
    original.add(checkpoint.relative_to(root).as_posix())
    if len(original) > MAX_ARTIFACT_COUNT:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_ARTIFACT_COUNT_EXCEEDED")

    store = _run_dir(root, vid, run_id) / STORE_NAME
    objects = _safe_directory(root, vid, store / "objects")
    manifests = _safe_directory(root, vid, store / "manifests")
    artifacts = []
    for relpath in sorted(original):
        src = _safe_path(root, vid, root / relpath)
        sha, size = _atomic_content_object(root, vid, objects, src=src)
        # A changing original may otherwise be snapshotted with contradictory hashes.
        if streaming_file_sha256(src) != (sha, size):
            raise Phase5FReceiptError("PHASE5F_RECEIPT_SOURCE_CHANGED")
        artifacts.append({"relpath": relpath, "sha256": sha, "size_bytes": size})
    payload = _manifest_payload(contract, artifacts)
    manifest_sha, _ = _atomic_content_object(root, vid, manifests, data=payload)
    if fault_after == "objects":
        raise Phase5FReceiptError("PHASE5F_INJECTED_CRASH_AFTER_OBJECTS")

    with closing(_connect(root)) as db:
        _contract_tables(db)
        _receipt_table(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            _assert_worker_lease_locked(db, contract, now)
            locked_events = _read_verified_stage_chain_locked(
                db, root, plan_sha, vid, verify_files_on_disk=True,
            )
            if len(locked_events) != len(MULTIMODAL_STAGES):
                raise Phase5FReceiptError("PHASE5F_RECEIPT_INCOMPLETE_STAGES")
            _verify_completed_run_artifacts_on_disk(root, vid, run_id, locked_events)
            # Lock-time file verification is necessary, not just staging-time hashing.
            for item in artifacts:
                path = _safe_path(root, vid, root / item["relpath"])
                if streaming_file_sha256(path) != (item["sha256"], item["size_bytes"]):
                    raise Phase5FReceiptError("PHASE5F_RECEIPT_SOURCE_CHANGED")
            row = db.execute(
                "SELECT manifest_sha FROM " + RECEIPT_TABLE
                + " WHERE plan_sha=? AND video_id=? AND run_id=?",
                (plan_sha, vid, run_id),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO " + RECEIPT_TABLE
                    + "(plan_sha,video_id,run_id,attempt,manifest_sha,committed_utc)"
                    + " VALUES (?,?,?,?,?,?)",
                    (plan_sha, vid, run_id, contract["attempt"], manifest_sha,
                     now.isoformat()),
                )
                replayed = False
            else:
                if row["manifest_sha"] != manifest_sha:
                    raise Phase5FReceiptError("PHASE5F_RECEIPT_CONFLICT")
                replayed = True
            db.commit()
        except Exception:
            db.rollback()
            raise

    if fault_after == "db_commit":
        raise Phase5FReceiptError("PHASE5F_INJECTED_CRASH_AFTER_COMMIT")
    return {
        "receipt_sha256": manifest_sha,
        "artifact_count": len(artifacts),
        "replayed": replayed,
    }


def verify_phase5f_receipt_locked(
    db, root: Path, contract: Mapping[str, Any], *,
    verify_sources: bool = True,
) -> dict[str, Any]:
    """Verify committed receipt under caller's transaction; no nested DB lock.

    Both the stage chain and the saved object set must match exactly. This
    check is called INSIDE the same SQLite writer transaction as settlement.
    """
    root = Path(root).resolve()
    plan_sha = str(contract["plan_sha256"])
    vid = str(contract["video_id"])
    run_id = str(contract["run_id"])
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (RECEIPT_TABLE,),
    ).fetchone()
    if not exists:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_NOT_COMMITTED")
    row = db.execute(
        "SELECT manifest_sha,attempt FROM " + RECEIPT_TABLE
        + " WHERE plan_sha=? AND video_id=? AND run_id=?",
        (plan_sha, vid, run_id),
    ).fetchone()
    if row is None or row["attempt"] != contract["attempt"]:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_NOT_COMMITTED")
    manifest_sha = str(row["manifest_sha"])
    if len(manifest_sha) != 64 or any(x not in "0123456789abcdef" for x in manifest_sha):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_MANIFEST_CORRUPTED")
    store = _run_dir(root, vid, run_id) / STORE_NAME
    manifest = _safe_path(root, vid, store / "manifests" / manifest_sha)
    if not manifest.is_file():
        raise Phase5FReceiptError("PHASE5F_RECEIPT_MANIFEST_MISSING")
    if manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_MANIFEST_CORRUPTED")
    if streaming_file_sha256(manifest)[0] != manifest_sha:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_MANIFEST_CORRUPTED")
    try:
        obj = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_MANIFEST_CORRUPTED") from None
    if (
        not isinstance(obj, dict) or obj.get("schema_version") != 1 or
        any(obj.get(key) != contract[key] for key in (
            "plan_sha256", "video_id", "run_id", "attempt"
        ))
    ):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_IDENTITY_MISMATCH")
    items = obj.get("artifacts")
    if not isinstance(items, list) or not items or len(items) > MAX_ARTIFACT_COUNT:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_ENTRIES_INVALID")

    try:
        events = _read_verified_stage_chain_locked(
            db, root, plan_sha, vid, verify_files_on_disk=verify_sources,
        )
    except WorkerContractError as exc:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_STAGE_INTEGRITY_FAILURE") from exc
    if len(events) != len(MULTIMODAL_STAGES) or any(
        x["run_id"] != run_id for x in events
    ):
        raise Phase5FReceiptError("PHASE5F_RECEIPT_STAGE_CHAIN_MISMATCH")
    expected = {
        str(item["relpath"])
        for event in events
        for item in event["output_manifest"]["artifacts"]
    }
    checkpoint = _run_dir(root, vid, run_id) / "multimodal_checkpoint.json"
    expected.add(checkpoint.relative_to(root).as_posix())

    names = set()
    for item in items:
        if not isinstance(item, dict):
            raise Phase5FReceiptError("PHASE5F_RECEIPT_ENTRIES_INVALID")
        name, sha, size = item.get("relpath"), item.get("sha256"), item.get("size_bytes")
        if (not isinstance(name, str) or name in names or
                not isinstance(sha, str) or len(sha) != 64 or
                any(x not in "0123456789abcdef" for x in sha) or
                type(size) is not int or not 0 <= size <= MAX_ARTIFACT_BYTES):
            raise Phase5FReceiptError("PHASE5F_RECEIPT_ENTRIES_INVALID")
        names.add(name)
        original = _safe_path(root, vid, root / name)
        snapshot = _safe_path(root, vid, store / "objects" / sha)
        if not snapshot.is_file() or streaming_file_sha256(snapshot) != (sha, size):
            raise Phase5FReceiptError("PHASE5F_RECEIPT_OBJECT_CORRUPTED")
        if verify_sources:
            if not original.is_file() or streaming_file_sha256(original) != (sha, size):
                raise Phase5FReceiptError("PHASE5F_RECEIPT_SOURCE_CHANGED")
    if names != expected:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_ARTIFACT_SET_MISMATCH")
    return {
        "receipt_sha256": manifest_sha,
        "artifact_count": len(items),
        "verified": True,
    }


def verify_phase5f_receipt(
    root: Path, contract: Mapping[str, Any], *,
    verify_sources: bool = True,
) -> dict[str, Any]:
    """Read-only verification; safe even after successful queue settlement."""
    with closing(_connect(Path(root).resolve())) as db:
        return verify_phase5f_receipt_locked(
            db, root, contract, verify_sources=verify_sources,
        )


def finish_phase5f_receipted_job(
    root: Path, contract: Mapping[str, Any], *,
    now_utc: datetime,
    usage_source: str,
) -> dict[str, Any]:
    """Resume after an acknowledged receipt using the ORIGINAL authorized lease.

    This function never creates a new claim and never auto-recovers an expired
    lease. The caller must retain the authorized contract outside Git/CI output.
    """
    if not isinstance(usage_source, str) or not usage_source:
        raise Phase5FReceiptError("PHASE5F_RECEIPT_USAGE_REQUIRED")
    verify_phase5f_receipt(root, contract, verify_sources=True)
    return finish_worker_contract_job(
        root, contract, success=True, usage_source=usage_source,
        now_utc=now_utc, require_phase5f_receipt=True,
    )
