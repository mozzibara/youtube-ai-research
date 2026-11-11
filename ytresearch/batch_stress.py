"""Isolated offline Phase 5D concurrency and rate-fault stress harness.

Creates a fresh TEMPORARY synthetic 77-source ledger and fake media-cache
placeholders per round. Never opens the user's production source registry,
never runs AGY, never downloads media, and never marks research completed.

The test deliberately holds the winning lease while other concurrent
threads try to claim. Exactly one claim may succeed per contention wave.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event
from typing import Any, Dict

from .batch_budget import (
    BatchBudgetError, batch_budget_status, configure_batch_budget,
    reconcile_paused_reservation, resume_global_rate_limit,
)
from .batch_checkpoint import (
    InjectedSyntheticCrash, record_synthetic_stage,
    synthetic_stage_status,
)
from .batch_plan import plan_research_batch
from .batch_queue import (
    BatchQueueError, batch_queue_status, claim_batch_job,
    finish_batch_job, initialize_batch_queue,
)
from .multimodal import MULTIMODAL_STAGES


MODEL = "gemini-3.8-flash-medium"
EXPECTED_CONCURRENT_ERROR = "QUEUE_ACTIVE_OR_EXPIRED_LEASE_REQUIRES_RELEASE"


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _create_isolated_fixture(root: Path) -> tuple[str, bytes]:
    (root / "research").mkdir()
    source = root / "research" / "index.json"
    source.write_text(json.dumps({
        "source_count": 77,
        "sources": [
            {
                "source_id": f"S{i:03d}", "source_ids": [f"S{i:03d}"],
                "video_id": f"{i:011d}", "playlist_position": i,
            }
            for i in range(1, 78)
        ],
    }), encoding="utf-8")
    original = source.read_bytes()
    for n in (1, 2, 3):
        vid = f"{n:011d}"
        folder = root / "data" / "videos" / vid
        folder.mkdir(parents=True)
        mp4 = folder / "mock_video.mp4"
        captions = folder / "mock_captions.json3"
        mp4.write_bytes(b"synthetic fixture; not a real video")
        captions.write_text("{}", encoding="utf-8")
        (folder / "media_cache.json").write_text(json.dumps({
            "assets": {
                "video": {"cached_path": str(mp4)},
                "subtitle_json3": {"cached_path": str(captions)},
            },
        }), encoding="utf-8")
    plan = plan_research_batch(root, max_jobs=3, save=True)
    sha = plan["plan_sha256"]
    initialize_batch_queue(root, sha)
    policy = {
        "schema_version": 1, "mode": "SYNTHETIC_DRYRUN_ONLY",
        "budget_id": "phase5d-stress",
        "global": {
            "max_tokens": 100000, "max_api_equivalent_usd": "0.50",
        },
        "models": {
            MODEL: {
                "max_tokens": 100000, "max_api_equivalent_usd": "0.50",
                "reserve": {
                    "uncached_input_tokens": 1000, "cache_read_tokens": 0,
                    "output_tokens": 100,
                },
            },
        },
        "rate_limit": {
            "max_requests_per_window": 2, "window_seconds": 60,
        },
    }
    path = root / "stress_policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    configure_batch_budget(
        root, sha, path,
        now_utc=datetime(2026, 11, 11, tzinfo=timezone.utc),
    )
    return sha, original


def run_phase5d_synthetic_stress(*, workers: int = 12,
                                  rounds: int = 3) -> Dict[str, Any]:
    """Deterministically check full queue/reservation/crash/429 invariants.

    8-32 concurrent workers and 1-10 rounds. Everything stays in temporary
    synthetic fixtures. Unexpected result raises AssertionError, not success.
    """
    if type(workers) is not int or not 2 <= workers <= 32:
        raise ValueError("STRESS_WORKERS_OUT_OF_RANGE")
    if type(rounds) is not int or not 1 <= rounds <= 10:
        raise ValueError("STRESS_ROUNDS_OUT_OF_RANGE")
    stats = {
        "mode": "SYNTHETIC_DRYRUN_ONLY",
        "rounds": rounds,
        "workers_per_round": workers,
        "claim_attempts": 0, "leases_granted_under_contention": 0,
        "leases_denied_by_lock": 0, "checkpoint_commit_crashes": 0,
        "checkpoint_idempotent_replays": 0,
        "simulated_429_global_pauses": 0,
        "blocked_global_claims": 0, "operator_reconciliations": 0,
        "simulated_jobs": 0,
        "actual_agy_calls": 0,
        "violations": 0,
    }
    for round_no in range(rounds):
        with tempfile.TemporaryDirectory(prefix="phase5d_synthetic_") as temp:
            root = Path(temp)
            sha, source_bytes = _create_isolated_fixture(root)
            t0 = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
            start = Event()

            def contend(i: int):
                start.wait(timeout=10)
                return claim_batch_job(
                    root, sha, owner=f"synthetic-worker-{i}", model=MODEL,
                    now_utc=t0,
                )

            results = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(contend, i) for i in range(workers)]
                start.set()
                for future in as_completed(futures):
                    try:
                        results.append(("LEASED", future.result()))
                    except BatchQueueError as exc:
                        if EXPECTED_CONCURRENT_ERROR not in str(exc):
                            raise
                        results.append(("BLOCKED", str(exc)))
            winners = [r for result, r in results if result == "LEASED"]
            denied = len(results) - len(winners)
            assert len(winners) == 1, f"Concurrent claim violation: {len(winners)}"
            assert denied == workers - 1, "Unexpected contention result"
            stats["claim_attempts"] += workers
            stats["leases_granted_under_contention"] += 1
            stats["leases_denied_by_lock"] += denied

            first = winners[0]
            video = first["video_id"]
            try:
                record_synthetic_stage(
                    root, sha, video, lease_token=first["lease_token"],
                    stage_name=MULTIMODAL_STAGES[0],
                    input_sha256=_h(f"{round_no}:in:0"),
                    output_sha256=_h(f"{round_no}:out:0"),
                    now_utc=t0, inject_fault="after_commit",
                )
                raise AssertionError("Expected after-commit injected crash")
            except InjectedSyntheticCrash:
                stats["checkpoint_commit_crashes"] += 1
            again = record_synthetic_stage(
                root, sha, video, lease_token=first["lease_token"],
                stage_name=MULTIMODAL_STAGES[0],
                input_sha256=_h(f"{round_no}:in:0"),
                output_sha256=_h(f"{round_no}:out:0"),
                now_utc=t0,
            )
            assert again["replayed_existing_checkpoint"], "Committed stage repeated"
            stats["checkpoint_idempotent_replays"] += 1
            for index, stage in enumerate(MULTIMODAL_STAGES[1:], 1):
                record_synthetic_stage(
                    root, sha, video, lease_token=first["lease_token"],
                    stage_name=stage,
                    input_sha256=_h(f"{round_no}:in:{index}"),
                    output_sha256=_h(f"{round_no}:out:{index}"),
                    now_utc=t0,
                )
            assert synthetic_stage_status(root, sha, video)["stages_done"] == len(MULTIMODAL_STAGES)
            result = finish_batch_job(
                root, sha, video_id=video,
                lease_token=first["lease_token"], success=True,
                usage={"uncached_input_tokens":200,"cache_read_tokens":0,
                       "output_tokens":20},
                now_utc=t0 + timedelta(seconds=1),
            )
            assert result["status"] == "SIMULATED", "No dry-run finish"
            stats["simulated_jobs"] += 1

            second = claim_batch_job(
                root, sha, owner="simulated-429-worker", model=MODEL,
                now_utc=t0 + timedelta(seconds=2),
            )
            fail = finish_batch_job(
                root, sha, video_id=second["video_id"],
                lease_token=second["lease_token"], success=False,
                rate_error_code="429", retry_after_seconds=90,
                now_utc=t0 + timedelta(seconds=3),
            )
            assert fail["global_rate_paused"], "429 failed to pause globally"
            stats["simulated_429_global_pauses"] += 1
            assert batch_budget_status(
                root, sha, now_utc=t0 + timedelta(seconds=3),
            )["global"]["unsettled_reservations"] == 1

            try:
                claim_batch_job(
                    root, sha, owner="blocked-by-429", model=MODEL,
                    now_utc=t0 + timedelta(seconds=95),
                )
                raise AssertionError("Global pause allowed a new claim")
            except BatchBudgetError as exc:
                assert "GLOBAL_RATE_LIMIT_PAUSED" in str(exc)
            stats["blocked_global_claims"] += 1
            reconcile_paused_reservation(
                root, sha, second["video_id"], actor="synthetic-operator",
                reason="Reconciled only the injected synthetic rate failure.",
                usage={"uncached_input_tokens":0, "cache_read_tokens":0,
                       "output_tokens":0},
                now_utc=t0 + timedelta(seconds=96),
            )
            stats["operator_reconciliations"] += 1
            resume_global_rate_limit(
                root, sha, actor="synthetic-operator",
                reason="Synthetic global cooldown elapsed with usage reconciled.",
                now_utc=t0 + timedelta(seconds=97),
            )
            third = claim_batch_job(
                root, sha, owner="synthetic-third-worker", model=MODEL,
                now_utc=t0 + timedelta(seconds=98),
            )
            finish_batch_job(
                root, sha, video_id=third["video_id"],
                lease_token=third["lease_token"], success=True,
                usage={"uncached_input_tokens":200,"cache_read_tokens":0,
                       "output_tokens":20},
                now_utc=t0 + timedelta(seconds=99),
            )
            stats["simulated_jobs"] += 1

            final_queue = batch_queue_status(root, sha)
            assert final_queue["counts"] == {
                "PAUSED_FAILURE": 1, "SIMULATED": 2,
            }, final_queue["counts"]
            budget = batch_budget_status(
                root, sha, now_utc=t0 + timedelta(seconds=100),
            )
            assert budget["global"]["unsettled_reservations"] == 0
            assert budget["global"]["used_and_reserved_tokens"] == 440
            assert not budget["rate_limit"]["global_paused"]
            assert (root / "research" / "index.json").read_bytes() == source_bytes
            assert not (root / "data" / "research_state.sqlite3").exists()
            assert not (root / "data" / "videos" / video / "runs").exists()
    return stats
