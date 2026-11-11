"""Phase 5F.4: read-only forensic audit of synthetic, private receipt objects.

No unlink, cleanup, lease mutation, model calls or public artifact publication.
Only aggregate counts and fixed action codes are returned, never file payloads.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
from typing import Any

from .batch_queue import _connect
from .batch_worker_contract import WorkerContractError
from .phase5f_offline import OFFLINE_ADAPTER_MODE, _require_synthetic_workspace
from .phase5f_recovery import _check_identity
from .phase5f_staging import (
    Phase5FReceiptError, RECEIPT_TABLE, STORE_NAME,
    _run_dir, _safe_path, verify_phase5f_receipt,
)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TEMP = re.compile(r"\.writing-[A-Za-z0-9_.-]+\.tmp\Z")
MAX_SCAN_FILES = 256


def _scan_folder(root: Path, video_id: str, folder: Path) -> tuple[set[str], int, int]:
    """Return immutable-like objects, temp files, unexpected entries.

    Do not follow symlink, do not read object bytes, and cap each directory
    to avoid an untrusted process creating an arbitrarily long audit.
    """
    if not folder.exists():
        return set(), 0, 0
    safe = _safe_path(root, video_id, folder)
    if safe.is_symlink() or not safe.is_dir():
        raise Phase5FReceiptError("PHASE5F_AUDIT_UNSAFE_DIRECTORY")
    hashes: set[str] = set()
    temps = 0
    unexpected = 0
    for n, candidate in enumerate(safe.iterdir()):
        if n >= MAX_SCAN_FILES:
            raise Phase5FReceiptError("PHASE5F_AUDIT_SCAN_LIMIT")
        if candidate.is_symlink() or not candidate.is_file():
            unexpected += 1
        elif _SHA.fullmatch(candidate.name):
            hashes.add(candidate.name)
        elif _TEMP.fullmatch(candidate.name):
            temps += 1
        else:
            unexpected += 1
    return hashes, temps, unexpected


def inspect_phase5f_offline_store(
    root: Path, plan_sha256: str, *, video_id: str, run_id: str,
) -> dict[str, Any]:
    """Summarize committed receipt and orphan files WITHOUT mutating storage.

    A content object can survive a simulated/process crash before SQLite
    commits. That is an orphan, not a completed job and never safe to publish.
    Even an intact committed receipt is merely a synthetic fixture receipt.
    """
    _check_identity(plan_sha256, video_id, run_id)
    root = Path(root).resolve()
    _require_synthetic_workspace(root)
    with closing(_connect(root)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (RECEIPT_TABLE,),
        ).fetchone()
        row = (db.execute(
            f"SELECT manifest_sha,attempt FROM {RECEIPT_TABLE} "
            "WHERE plan_sha=? AND video_id=? AND run_id=?",
            (plan_sha256, video_id, run_id),
        ).fetchone() if exists else None)
        receipt = dict(row) if row is not None else None

    def result(code: str, *, objects=0, orphans=0, manifests=0,
               temp_files=0, unexpected=0, verified=False):
        return {
            "mode": OFFLINE_ADAPTER_MODE,
            "action_code": code,
            "committed_receipt": receipt is not None,
            "receipt_verified": verified,
            "object_count": objects,
            "unreferenced_object_count": orphans,
            "unreferenced_manifest_count": manifests,
            "temporary_file_count": temp_files,
            "unexpected_file_count": unexpected,
            "auto_cleanup_enabled": False,
            "external_calls_enabled": False,
            "credentials_exposed": False,
        }

    try:
        run = _run_dir(root, video_id, run_id)
        store = _safe_path(root, video_id, run / STORE_NAME)
        if not store.exists():
            if receipt is not None:
                return result("COMMITTED_RECEIPT_CORRUPT")
            return result("STORE_ABSENT")
        if store.is_symlink() or not store.is_dir():
            return result("STORE_UNSAFE")
        objects_dir = _safe_path(root, video_id, store / "objects")
        manifests_dir = _safe_path(root, video_id, store / "manifests")
        objects, tmp1, bad1 = _scan_folder(root, video_id, objects_dir)
        manifests, tmp2, bad2 = _scan_folder(root, video_id, manifests_dir)
    except (Phase5FReceiptError, WorkerContractError, OSError):
        return result("STORE_UNSAFE")

    temp_count = tmp1 + tmp2
    unexpected_count = bad1 + bad2
    if unexpected_count:
        return result(
            "STORE_UNSAFE", objects=len(objects), temp_files=temp_count,
            unexpected=unexpected_count,
        )
    if receipt is None:
        if objects or manifests or temp_count:
            return result(
                "UNCOMMITTED_OBJECTS", objects=len(objects),
                orphans=len(objects), manifests=len(manifests),
                temp_files=temp_count,
            )
        return result("STORE_EMPTY")

    manifest_name = str(receipt["manifest_sha"])
    referenced = set()
    verified = False
    if _SHA.fullmatch(manifest_name) and manifest_name in manifests:
        try:
            proof = {
                "plan_sha256": plan_sha256, "video_id": video_id,
                "run_id": run_id, "attempt": receipt["attempt"],
            }
            verify_phase5f_receipt(root, proof, verify_sources=True)
            path = _safe_path(root, video_id, manifests_dir / manifest_name)
            manifest_data = json.loads(path.read_text(encoding="utf-8"))
            referenced = {str(x["sha256"]) for x in manifest_data["artifacts"]}
            verified = True
        except (Phase5FReceiptError, WorkerContractError, OSError,
                ValueError, KeyError, TypeError):
            verified = False

    return result(
        "COMMITTED_RECEIPT_VERIFIED" if verified else "COMMITTED_RECEIPT_CORRUPT",
        objects=len(objects),
        orphans=len(objects - referenced) if verified else len(objects),
        manifests=len(manifests - {manifest_name}),
        temp_files=temp_count,
        verified=verified,
    )
