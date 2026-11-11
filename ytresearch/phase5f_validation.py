"""Phase 5F.5: reproducible standalone synthetic-only preflight for CI/owners.

Creates isolated throwaway workspaces, never reads --root/private research,
never calls providers, and emits only fixed scenario IDs and aggregate results.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path
import tempfile
from typing import Any, Callable

from .batch_budget import batch_budget_status
from .batch_queue import batch_queue_status, claim_batch_job
from .batch_worker_contract import (
    DEFAULT_FLASH_MODEL, build_worker_input_contract,
    create_phase5e_isolated_workspace, materialize_and_record_nine_stages,
    offline_external_call_guard,
)
from .phase5f_audit import inspect_phase5f_offline_store
from .phase5f_offline import (
    OFFLINE_ADAPTER_MODE, Phase5FOfflineError,
    SyntheticNineStageAdapter, run_phase5f_offline_next,
)
from .phase5f_recovery import (
    Phase5FRecoveryError, inspect_phase5f_offline_recovery,
    resume_phase5f_offline_receipt,
)
from .phase5f_staging import (
    Phase5FReceiptError, _atomic_content_object, _safe_directory,
    stage_phase5f_receipt,
)


class Phase5FPreflightError(RuntimeError):
    """No raw exception text is ever forwarded to a public check summary."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise Phase5FPreflightError(code)


def _build_claim(ws: dict[str, Any], run_id: str):
    root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
    claim = claim_batch_job(
        root, sha, owner="p5f5-offline", model=DEFAULT_FLASH_MODEL, now_utc=t0,
    )
    contract = build_worker_input_contract(
        root, plan_sha256=sha, video_id=claim["video_id"], run_id=run_id,
        exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
        lease_expires_utc=claim["lease_expires_utc"],
        attempt=1, owner="p5f5-offline",
    )
    materialize_and_record_nine_stages(
        root, contract, now_utc=t0 + timedelta(seconds=1),
    )
    return claim, contract


def _audit(ws: dict[str, Any], run_id: str):
    return inspect_phase5f_offline_store(
        ws["root"], ws["plan_sha256"],
        video_id=f"{1:011d}", run_id=run_id,
    )


def _normal_settlement(ws: dict[str, Any]) -> str:
    run_id = "preflight_normal"
    res = run_phase5f_offline_next(
        ws["root"], ws["plan_sha256"], owner="p5f5-offline",
        run_id=run_id, now_utc=ws["t0"],
    )
    state = _audit(ws, run_id)
    _require(res["status"] == "SIMULATED", "P5F5_NORMAL_NOT_SIMULATED")
    _require(state["action_code"] == "COMMITTED_RECEIPT_VERIFIED",
             "P5F5_NORMAL_RECEIPT_FAILED")
    _require(state["unreferenced_object_count"] == 0,
             "P5F5_NORMAL_ORPHANED_OBJECTS")
    _require(res["actual_agy_calls"] == 0 and res["actual_youtube_calls"] == 0,
             "P5F5_NETWORK_FORBIDDEN")
    _require(
        batch_budget_status(ws["root"], ws["plan_sha256"])
        ["global"]["unsettled_reservations"] == 0,
        "P5F5_NORMAL_BUDGET_NOT_SETTLED",
    )
    return "SIMULATED_RECEIPTED"


def _rate_limit_pause(ws: dict[str, Any]) -> str:
    res = run_phase5f_offline_next(
        ws["root"], ws["plan_sha256"], owner="p5f5-offline",
        run_id="preflight_rate_429", now_utc=ws["t0"],
        adapter=SyntheticNineStageAdapter(simulate_rate_limit=True),
    )
    budget = batch_budget_status(ws["root"], ws["plan_sha256"])
    _require(res["status"] == "PAUSED_FAILURE", "P5F5_RATE_NOT_PAUSED")
    _require(budget["rate_limit"]["global_paused"],
             "P5F5_RATE_GLOBAL_PAUSE_MISSING")
    _require(budget["global"]["unsettled_reservations"] == 1,
             "P5F5_RATE_RESERVATION_RELEASED")
    return "GLOBAL_PAUSE_RESERVED"


def _reject_real_media(ws: dict[str, Any]) -> str:
    vid = f"{1:011d}"
    media = ws["root"] / "data" / "videos" / vid / "media" / "video.mp4"
    media.write_bytes(b"not-an-approved-synthetic-video")
    try:
        run_phase5f_offline_next(
            ws["root"], ws["plan_sha256"], owner="p5f5-offline",
            run_id="preflight_reject_media", now_utc=ws["t0"],
        )
    except Phase5FOfflineError:
        pass
    else:
        raise Phase5FPreflightError("P5F5_REAL_MEDIA_ACCEPTED")
    _require(
        batch_queue_status(ws["root"], ws["plan_sha256"])["counts"] == {"PENDING": 3},
        "P5F5_REAL_MEDIA_CLAIMED",
    )
    return "MEDIA_REJECTED_BEFORE_CLAIM"


def _orphan_precommit(ws: dict[str, Any]) -> str:
    run_id = "preflight_crash_before"
    _, contract = _build_claim(ws, run_id)
    try:
        stage_phase5f_receipt(
            ws["root"], contract,
            now_utc=ws["t0"] + timedelta(seconds=12),
            fault_after="objects",
        )
    except Phase5FReceiptError as exc:
        _require(str(exc) == "PHASE5F_INJECTED_CRASH_AFTER_OBJECTS",
                 "P5F5_PRECOMMIT_WRONG_FAILURE")
    else:
        raise Phase5FPreflightError("P5F5_PRECOMMIT_NO_FAILURE")
    info = _audit(ws, run_id)
    _require(info["action_code"] == "UNCOMMITTED_OBJECTS" and
             info["unreferenced_object_count"] > 0,
             "P5F5_PRECOMMIT_ORPHAN_NOT_RECOGNIZED")
    return "ORPHAN_UNCOMMITTED"


def _recover_postcommit(ws: dict[str, Any]) -> str:
    run_id = "preflight_crash_after"
    claim, contract = _build_claim(ws, run_id)
    try:
        stage_phase5f_receipt(
            ws["root"], contract,
            now_utc=ws["t0"] + timedelta(seconds=12),
            fault_after="db_commit",
        )
    except Phase5FReceiptError as exc:
        _require(str(exc) == "PHASE5F_INJECTED_CRASH_AFTER_COMMIT",
                 "P5F5_POSTCOMMIT_WRONG_FAILURE")
    else:
        raise Phase5FPreflightError("P5F5_POSTCOMMIT_NO_FAILURE")
    state = inspect_phase5f_offline_recovery(
        ws["root"], ws["plan_sha256"], video_id=f"{1:011d}",
        run_id=run_id, now_utc=ws["t0"] + timedelta(seconds=20),
    )
    _require(state["action_code"] == "ORIGINAL_LEASE_PROOF_REQUIRED",
             "P5F5_POSTCOMMIT_RECOVERY_NOT_READY")
    result = resume_phase5f_offline_receipt(
        ws["root"], ws["plan_sha256"], video_id=f"{1:011d}",
        run_id=run_id, owner="p5f5-offline",
        lease_token=claim["lease_token"],
        now_utc=ws["t0"] + timedelta(seconds=20),
    )
    _require(result["status"] == "SIMULATED", "P5F5_REPLAY_NOT_SETTLED")
    _require(claim["lease_token"] not in json.dumps(result),
             "P5F5_REPLAY_TOKEN_EXPOSED")
    try:
        resume_phase5f_offline_receipt(
            ws["root"], ws["plan_sha256"], video_id=f"{1:011d}",
            run_id=run_id, owner="p5f5-offline",
            lease_token=claim["lease_token"],
            now_utc=ws["t0"] + timedelta(seconds=21),
        )
    except Phase5FRecoveryError:
        pass
    else:
        raise Phase5FPreflightError("P5F5_DOUBLE_SETTLEMENT_ACCEPTED")
    return "REPLAY_SETTLED_ONCE"


def _detect_corruption(ws: dict[str, Any]) -> str:
    run_id = "preflight_corruption"
    _, contract = _build_claim(ws, run_id)
    receipt = stage_phase5f_receipt(
        ws["root"], contract,
        now_utc=ws["t0"] + timedelta(seconds=12),
    )
    store = (ws["root"] / "data" / "videos" / f"{1:011d}"
             / "runs" / run_id / ".phase5f1")
    manifest = json.loads(
        (store / "manifests" / receipt["receipt_sha256"])
        .read_text(encoding="utf-8")
    )
    (store / "objects" / manifest["artifacts"][0]["sha256"]).write_bytes(
        b"intentionally-corrupted-synthetic-object"
    )
    _require(_audit(ws, run_id)["action_code"] == "COMMITTED_RECEIPT_CORRUPT",
             "P5F5_CORRUPT_RECEIPT_ACCEPTED")
    return "CORRUPTION_BLOCKED"


def _concurrent_no_clobber(ws: dict[str, Any]) -> str:
    vid = f"{1:011d}"
    root = ws["root"]
    path = root / "data" / "videos" / vid / "runs" / "preflight_concurrent"
    store = _safe_directory(root, vid, path / ".phase5f1" / "objects")
    payload = b"phase5f5-synthetic-shared-content"
    def write(_):
        return _atomic_content_object(root, vid, store, data=payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(6)))
    _require(len(set(results)) == 1, "P5F5_PARALLEL_HASH_DISAGREEMENT")
    _require(len(list(store.iterdir())) == 1, "P5F5_PARALLEL_ORPHAN_TEMPS")
    return "PARALLEL_NO_CLOBBER"


def run_phase5f_validation_suite() -> dict[str, Any]:
    """Perform fixed isolated checks; no external I/O or user-supplied files."""
    cases: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
        ("F01", _normal_settlement),
        ("F02", _rate_limit_pause),
        ("F03", _reject_real_media),
        ("F04", _orphan_precommit),
        ("F05", _recover_postcommit),
        ("F06", _detect_corruption),
        ("F07", _concurrent_no_clobber),
    ]
    outcomes = []
    with offline_external_call_guard() as tracker:
        for scenario_id, case in cases:
            try:
                with tempfile.TemporaryDirectory(prefix="p5f5_safe_") as temporary:
                    ws = create_phase5e_isolated_workspace(Path(temporary))
                    code = case(ws)
            except Exception:
                # Do not include arbitrary exception messages, paths, tokens
                # or model outputs in the public CLI/Actions report.
                outcomes.append({
                    "id": scenario_id, "status": "FAIL",
                    "result_code": "P5F5_SCENARIO_FAILED",
                })
            else:
                outcomes.append({
                    "id": scenario_id, "status": "PASS",
                    "result_code": code,
                })
    blocked = tracker.blocked_unauthorized_attempts
    if blocked:
        outcomes.append({
            "id": "F08", "status": "FAIL",
            "result_code": "P5F5_UNAUTHORIZED_EXTERNAL_CALL_ATTEMPT",
        })
    passed = sum(x["status"] == "PASS" for x in outcomes)
    return {
        "mode": OFFLINE_ADAPTER_MODE,
        "total_scenarios": len(outcomes),
        "passed_scenarios": passed,
        "failed_scenarios": len(outcomes) - passed,
        "external_calls_enabled": False,
        "actual_agy_calls": tracker.live_agy_calls,
        "actual_youtube_calls": tracker.youtube_calls,
        "unauthorized_call_attempts": blocked,
        "human_approval": False,
        "live_processing_completed": False,
        "scenarios": outcomes,
    }
