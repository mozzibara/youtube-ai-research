"""Phase 5F.0: injected, synthetic-only worker adapter (never a live processor)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from .batch_queue import _now, batch_queue_status, claim_batch_job
from .batch_worker_contract import (
    DEFAULT_FLASH_MODEL,
    build_synthetic_stream_json_usage,
    build_worker_input_contract,
    finish_worker_contract_job,
    materialize_and_record_nine_stages,
    offline_external_call_guard,
    validate_worker_input_contract,
)
from .research_state import RUN_ID
from .storage import read_json

OFFLINE_ADAPTER_MODE = "SYNTHETIC_DRYRUN_ONLY"


class Phase5FOfflineError(RuntimeError):
    """Constant error codes only; never include raw source data or lease tokens."""


class StageAdapter(Protocol):
    execution_mode: str

    def execute(self, root: Path, contract: Mapping[str, Any], *,
                now_utc: datetime) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class SyntheticNineStageAdapter:
    execution_mode: str = OFFLINE_ADAPTER_MODE
    simulate_rate_limit: bool = False
    retry_after_seconds: int = 60

    def execute(self, root: Path, contract: Mapping[str, Any], *,
                now_utc: datetime) -> Mapping[str, Any]:
        if self.simulate_rate_limit:
            return {
                "kind": "RATE_LIMITED",
                "provider_error_code": "429",
                "retry_after_seconds": self.retry_after_seconds,
            }
        materialize_and_record_nine_stages(root, contract, now_utc=now_utc)
        return {
            "kind": "SIMULATED_SUCCESS",
            "usage_source": build_synthetic_stream_json_usage(
                model=str(contract["exact_model_id"])
            ),
        }


def _require_synthetic_workspace(root: Path) -> None:
    """Fail closed on any real/unrecognized input before a lease is claimed."""
    index = root / "research" / "index.json"
    if not index.is_file() or index.is_symlink():
        raise Phase5FOfflineError("PHASE5F_SYNTHETIC_FIXTURE_REQUIRED")
    try:
        rows = read_json(index)["sources"]
        if len(rows) != 77:
            raise ValueError("not isolated")
        for n, item in enumerate(rows, 1):
            if (item.get("video_id") != f"{n:011d}" or
                    item.get("source_id") != f"S{n:03d}" or
                    item.get("source_ids") != [f"S{n:03d}"]):
                raise ValueError("non-fixture source")
        for n in (1, 2, 3):
            vid = f"{n:011d}"
            media = root / "data" / "videos" / vid / "media"
            video, sub = media / "video.mp4", media / "captions.json3"
            expected = f"isolated-offline-video-bytes-{vid}".encode("utf-8")
            if (media.is_symlink() or video.is_symlink() or sub.is_symlink() or
                    not video.is_file() or video.stat().st_size != len(expected) or
                    video.read_bytes() != expected):
                raise ValueError("non-fixture media")
            if not sub.is_file() or sub.stat().st_size > 1024:
                raise ValueError("non-fixture subtitle")
            cues = json.loads(sub.read_text(encoding="utf-8"))["events"]
            if (len(cues) != 2 or any(
                not str(e["segs"][0]["utf8"]).startswith("오프라인 계약 테스트 자막 ")
                for e in cues
            )):
                raise ValueError("non-fixture subtitle")
    except (OSError, ValueError, KeyError, TypeError, IndexError, UnicodeError):
        raise Phase5FOfflineError("PHASE5F_SYNTHETIC_FIXTURE_REQUIRED") from None


def run_phase5f_offline_next(
    root: Path, plan_sha256: str, *,
    owner: str,
    run_id: str,
    exact_model_id: str = DEFAULT_FLASH_MODEL,
    adapter: StageAdapter | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Claim one synthetic job; no automatic retry/recovery or live I/O."""
    driver: StageAdapter = adapter if adapter is not None else SyntheticNineStageAdapter()
    if (getattr(driver, "execution_mode", None) != OFFLINE_ADAPTER_MODE or
            not callable(getattr(driver, "execute", None))):
        raise Phase5FOfflineError("PHASE5F_LIVE_ADAPTER_FORBIDDEN")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise Phase5FOfflineError("PHASE5F_RUN_ID_INVALID")
    if not isinstance(owner, str) or not owner.strip():
        raise Phase5FOfflineError("PHASE5F_OWNER_REQUIRED")
    if (not isinstance(exact_model_id, str) or
            exact_model_id in ("auto", "LIVE")):
        raise Phase5FOfflineError("PHASE5F_EXACT_MODEL_REQUIRED")

    root = Path(root).resolve()
    _require_synthetic_workspace(root)
    now = _now(now_utc)
    with offline_external_call_guard() as tracker:
        claim = claim_batch_job(
            root, plan_sha256, owner=owner, model=exact_model_id, now_utc=now,
        )
        video_id = claim["video_id"]
        queue = batch_queue_status(root, plan_sha256)
        selected = next(x for x in queue["jobs"] if x["video_id"] == video_id)
        contract = build_worker_input_contract(
            root, plan_sha256=plan_sha256, video_id=video_id,
            run_id=run_id, exact_model_id=exact_model_id,
            lease_token=claim["lease_token"],
            lease_expires_utc=claim["lease_expires_utc"],
            attempt=selected["attempts"], owner=owner,
        )
        validate_worker_input_contract(root, contract, now_utc=now)
        outcome = driver.execute(root, contract, now_utc=now + timedelta(seconds=1))
        if not isinstance(outcome, Mapping):
            raise Phase5FOfflineError("PHASE5F_ADAPTER_RESULT_INVALID")
        if outcome.get("kind") == "RATE_LIMITED":
            delay = outcome.get("retry_after_seconds")
            if (outcome.get("provider_error_code") != "429" or
                    type(delay) is not int or not 0 <= delay <= 3600):
                raise Phase5FOfflineError("PHASE5F_RATE_ERROR_INVALID")
            result = finish_worker_contract_job(
                root, contract, success=False, provider_error_code="429",
                retry_after_seconds=delay, now_utc=now + timedelta(seconds=20),
            )
        elif outcome.get("kind") == "SIMULATED_SUCCESS":
            usage = outcome.get("usage_source")
            if not isinstance(usage, str) or not usage:
                raise Phase5FOfflineError("PHASE5F_USAGE_REQUIRED")
            result = finish_worker_contract_job(
                root, contract, success=True, usage_source=usage,
                now_utc=now + timedelta(seconds=20),
            )
        else:
            raise Phase5FOfflineError("PHASE5F_ADAPTER_RESULT_INVALID")
        return {
            "mode": OFFLINE_ADAPTER_MODE,
            "status": result["status"],
            "video_id": video_id,
            "run_id": run_id,
            "live_agy_enabled": False,
            "external_calls_blocked": True,
            "actual_agy_calls": tracker.live_agy_calls,
            "actual_youtube_calls": tracker.youtube_calls,
            "private_data_published": False,
        }
