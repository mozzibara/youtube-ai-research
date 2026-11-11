"""Phase 5F.3: explicit, synthetic-only operator resume after receipt commit.

No automatic retries, lease renewal, token persistence or real provider calls.
The caller must supply the original private lease proof again after restart.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import secrets
from pathlib import Path
from typing import Any

from .batch_queue import PLAN_PATTERN, _connect, _now
from .batch_worker_contract import (
    DEFAULT_FLASH_MODEL,
    WorkerContractError,
    build_synthetic_stream_json_usage,
    build_worker_input_contract,
    offline_external_call_guard,
    validate_worker_input_contract,
)
from .phase5f_offline import OFFLINE_ADAPTER_MODE, _require_synthetic_workspace
from .phase5f_staging import (
    Phase5FReceiptError,
    RECEIPT_TABLE,
    finish_phase5f_receipted_job,
    verify_phase5f_receipt,
)
from .research_state import RUN_ID, VIDEO_ID


class Phase5FRecoveryError(RuntimeError):
    """Only constant error codes; raw tokens, URLs and media are never returned."""


def _check_identity(plan_sha256: str, video_id: str, run_id: str) -> None:
    if not isinstance(plan_sha256, str) or not PLAN_PATTERN.fullmatch(plan_sha256):
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_PLAN_INVALID")
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_VIDEO_INVALID")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_RUN_INVALID")


def _queue_row(root: Path, plan_sha256: str, video_id: str):
    with closing(_connect(root)) as db:
        row = db.execute(
            "SELECT status,attempts,owner,lease_token,lease_expires_utc "
            "FROM queue_jobs WHERE plan_sha=? AND video_id=?",
            (plan_sha256, video_id),
        ).fetchone()
        return dict(row) if row is not None else None


def inspect_phase5f_offline_recovery(
    root: Path, plan_sha256: str, *, video_id: str,
    run_id: str, now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Read-only, token-free assessment of the next required operator action."""
    _check_identity(plan_sha256, video_id, run_id)
    root = Path(root).resolve()
    _require_synthetic_workspace(root)
    now = _now(now_utc)
    row = _queue_row(root, plan_sha256, video_id)
    if row is None:
        code = "UNKNOWN_JOB"
    elif row["status"] == "SIMULATED":
        code = "ALREADY_SETTLED"
    elif row["status"] == "PENDING":
        code = "NOT_CLAIMED"
    elif row["status"] != "LEASED":
        code = "MANUAL_PAUSE_OR_RECOVERY"
    else:
        try:
            expiry = datetime.fromisoformat(str(row["lease_expires_utc"]))
            if expiry.tzinfo is None:
                raise ValueError("naive expiry")
            expired = expiry <= now
        except (ValueError, TypeError):
            code = "LEASE_METADATA_INVALID"
        else:
            if expired:
                code = "LEASE_EXPIRED_MANUAL_RECOVERY"
            else:
                with closing(_connect(root)) as db:
                    exists = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (RECEIPT_TABLE,),
                    ).fetchone()
                    committed = bool(exists and db.execute(
                        "SELECT 1 FROM " + RECEIPT_TABLE
                        + " WHERE plan_sha=? AND video_id=? AND run_id=? "
                        "AND attempt=?",
                        (plan_sha256, video_id, run_id, row["attempts"]),
                    ).fetchone())
                if not committed:
                    code = "RECEIPT_ABSENT_REVALIDATION_REQUIRED"
                else:
                    proof = {
                        "plan_sha256": plan_sha256,
                        "video_id": video_id,
                        "run_id": run_id,
                        "attempt": row["attempts"],
                    }
                    try:
                        verify_phase5f_receipt(root, proof, verify_sources=True)
                    except (Phase5FReceiptError, WorkerContractError, OSError):
                        code = "RECEIPT_INTEGRITY_BLOCKED"
                    else:
                        code = "ORIGINAL_LEASE_PROOF_REQUIRED"

    return {
        "mode": OFFLINE_ADAPTER_MODE,
        "video_id": video_id,
        "run_id": run_id,
        "action_code": code,
        "auto_recovery_enabled": False,
        "tokens_exposed": False,
        "live_agy_enabled": False,
    }


def resume_phase5f_offline_receipt(
    root: Path, plan_sha256: str, *,
    video_id: str,
    run_id: str,
    owner: str,
    lease_token: str,
    exact_model_id: str = DEFAULT_FLASH_MODEL,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Manually resume an already receipted synthetic job with original lease.

    This API never claims a new job or changes an expired lease. A failed
    attempt retains the existing budget reservation for operator resolution.
    The supplied lease_token is used for constant-time verification but never
    written to a receipt or included in this API's output.
    """
    _check_identity(plan_sha256, video_id, run_id)
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 120:
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_OWNER_INVALID")
    if (not isinstance(lease_token, str) or len(lease_token) != 48 or
            any(ch not in "0123456789abcdef" for ch in lease_token)):
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_LEASE_PROOF_INVALID")
    if (not isinstance(exact_model_id, str) or not exact_model_id.strip() or
            exact_model_id == "auto"):
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_MODEL_INVALID")

    root = Path(root).resolve()
    _require_synthetic_workspace(root)
    now = _now(now_utc)
    row = _queue_row(root, plan_sha256, video_id)
    if row is None or row["status"] != "LEASED":
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_NO_ACTIVE_LEASE")
    if row["owner"] != owner or not secrets.compare_digest(
        str(row["lease_token"] or ""), lease_token
    ):
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_LEASE_PROOF_INVALID")
    expiry = str(row["lease_expires_utc"] or "")
    try:
        expiration = datetime.fromisoformat(expiry)
        if expiration.tzinfo is None:
            raise ValueError("naive expiration")
    except (ValueError, TypeError):
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_LEASE_METADATA_INVALID") from None
    if expiration <= now:
        raise Phase5FRecoveryError("PHASE5F_RECOVERY_LEASE_EXPIRED")

    with offline_external_call_guard() as tracker:
        contract = build_worker_input_contract(
            root, plan_sha256=plan_sha256, video_id=video_id,
            run_id=run_id, exact_model_id=exact_model_id,
            lease_token=lease_token, lease_expires_utc=expiry,
            attempt=row["attempts"], owner=owner,
        )
        validate_worker_input_contract(root, contract, now_utc=now)
        try:
            verify_phase5f_receipt(root, contract, verify_sources=True)
        except (Phase5FReceiptError, WorkerContractError) as exc:
            raise Phase5FRecoveryError("PHASE5F_RECOVERY_RECEIPT_NOT_VERIFIED") from exc
        result = finish_phase5f_receipted_job(
            root, contract, now_utc=now,
            usage_source=build_synthetic_stream_json_usage(
                model=exact_model_id
            ),
        )
        return {
            "mode": OFFLINE_ADAPTER_MODE,
            "video_id": video_id,
            "run_id": run_id,
            "status": result["status"],
            "recovered_with_original_lease": True,
            "tokens_exposed": False,
            "live_agy_enabled": False,
            "actual_agy_calls": tracker.live_agy_calls,
            "actual_youtube_calls": tracker.youtube_calls,
        }
