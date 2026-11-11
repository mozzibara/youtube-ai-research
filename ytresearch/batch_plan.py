"""Phase 5: deterministic OFFLINE multi-video work plan, never execution.

Read the verified 77-source ledger, existing SQLite runs and local cached
input manifests. Select a bounded queue without touching models, network,
videos, source index, user git state, checkpoints, or report approvals.

No live-worker entrypoint exists in this module; the output is PLAN_ONLY.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List
from urllib.parse import quote

from .research_state import read_source_ledger
from .storage import atomic_write, json_text, read_json


class BatchPlanBlocked(ValueError):
    """Ledger, read-only database or plan output violates invariants."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _readonly_state(root: Path) -> Dict[str, Dict[str, Any]]:
    path = Path(root) / "data" / "research_state.sqlite3"
    if not path.is_file():
        return {}
    uri = "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            runs = db.execute(
                "SELECT video_id,run_id,status FROM analysis_runs ORDER BY video_id,run_id"
            ).fetchall()
            reports = db.execute(
                "SELECT DISTINCT video_id FROM reports WHERE report_kind='RESEARCH_19' "
                "AND review_state IN ('APPROVED','PUBLISHED')"
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise BatchPlanBlocked("BATCH_SQLITE_READONLY_ERROR:" + type(exc).__name__) from exc
    result = defaultdict(lambda: {"runs": [], "approved": False})
    for row in runs:
        result[row["video_id"]]["runs"].append({
            "run_id": row["run_id"], "status": row["status"]
        })
    for row in reports:
        result[row["video_id"]]["approved"] = True
    return dict(result)


def _local_inputs(root: Path, video_id: str) -> Dict[str, Any]:
    """No automatic downloads; never include local/private file paths in output."""
    cache = root / "data" / "videos" / video_id / "media_cache.json"
    if not cache.is_file():
        return {"status": "MEDIA_CACHE_MISSING", "video": False, "subtitle_json3": False}
    try:
        info = read_json(cache)
        assets = info.get("assets", {})
        if not isinstance(assets, dict):
            raise ValueError("assets expected dict")
        def file_available(name):
            row = assets.get(name)
            return bool(
                isinstance(row, dict) and isinstance(row.get("cached_path"), str)
                and Path(row["cached_path"]).is_file()
            )
        video = file_available("video")
        captions = file_available("subtitle_json3")
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {"status": "INVALID_MEDIA_CACHE", "video": False, "subtitle_json3": False}
    return {
        "status": "READY" if (video and captions) else "MEDIA_INPUTS_INCOMPLETE",
        "video": video, "subtitle_json3": captions,
    }


def plan_research_batch(
    root: Path, *, expected: int = 77, max_jobs: int = 3,
    retry_failed: bool = False, save: bool = False,
) -> Dict[str, Any]:
    if not 1 <= max_jobs <= 10:
        raise BatchPlanBlocked("BATCH_MAX_JOBS_RANGE_1_TO_10")
    if expected < 1:
        raise BatchPlanBlocked("BATCH_EXPECTED_SOURCE_COUNT_INVALID")
    root = Path(root).resolve()
    verified = read_source_ledger(root, expected=expected)
    db = _readonly_state(root)
    entries = verified["entries"]
    by_video = defaultdict(list)
    for entry in entries:
        by_video[entry["video_id"]].append(entry)
    seen, planned, rows = set(), 0, []
    for entry in sorted(entries, key=lambda row: row["position"]):
        video_id = entry["video_id"]
        if video_id in seen:
            continue
        seen.add(video_id)
        aliases = by_video[video_id]
        state = db.get(video_id, {"approved": False, "runs": []})
        statuses = {run["status"] for run in state["runs"]}
        inputs = _local_inputs(root, video_id)
        if state["approved"]:
            disposition = "SKIP_APPROVED"
        elif "COMPLETED" in statuses:
            disposition = "SKIP_COMPLETED_REVIEW_REQUIRED"
        elif "IN_PROGRESS" in statuses:
            disposition = "HOLD_IN_PROGRESS"
        elif "INCONSISTENT" in statuses:
            disposition = "HOLD_INCONSISTENT"
        elif "FAILED" in statuses and not retry_failed:
            disposition = "HOLD_FAILED_NEEDS_EXPLICIT_RETRY"
        elif inputs["status"] != "READY":
            disposition = "HOLD_" + inputs["status"]
        elif planned >= max_jobs:
            disposition = "DEFER_CAP"
        else:
            disposition = "PLAN_ONLY"
            planned += 1
        rows.append({
            "video_id": video_id,
            "source_ids": [row["source_id"] for row in aliases],
            "source_positions": [row["position"] for row in aliases],
            "disposition": disposition,
            "local_inputs": inputs,
            "existing_run_statuses": sorted(statuses),
            "approved_report_exists": state["approved"],
        })
    counts = dict(sorted(Counter(x["disposition"] for x in rows).items()))
    approved_count = sum(row["approved_report_exists"] for row in rows)
    data = {
        "schema_version": 1,
        "execution_mode": "PLAN_ONLY_NO_MODEL_OR_NETWORK",
        "production_live_batch_authorized": False,
        "live_gate": "PILOT_REPORT_APPROVAL_REQUIRED" if approved_count == 0
                     else "SEPARATE_OPERATOR_APPROVAL_REQUIRED",
        "source_origin": verified["origin"],
        "source_entries": verified["entry_count"],
        "unique_videos": len(rows),
        "max_jobs": max_jobs, "retry_failed_requested": bool(retry_failed),
        "approved_video_count": approved_count,
        "disposition_counts": counts,
        "plan_only_video_ids": [row["video_id"] for row in rows
                                if row["disposition"] == "PLAN_ONLY"],
        "jobs": rows,
        "notes": (
            "This read-only selection DOES NOT run or download anything. "
            "Old completed/review-required runs are skipped; failed/inconsistent "
            "and in-progress jobs stay held by default. Files and source IDs "
            "are never modified. Running this command is not a live batch opt-in."
        ),
    }
    # Deterministic digest excludes a time clock and any private absolute paths.
    digest = _sha(json_text(data).encode("utf-8"))
    data["plan_sha256"] = digest
    if save:
        folder = root / "data" / "batch_plans"
        path = folder / f"batch_plan_{digest[:16]}.json"
        content = json_text(data)
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise BatchPlanBlocked("BATCH_PLAN_DIGEST_COLLISION_OR_MUTATION")
        else:
            atomic_write(path, content)
        data["saved_plan_path"] = str(path)
    return data
