"""Phase 5E: pre-integration worker safety contract and offline artifact/usage validation.

Bridges the Phase 5A-5D synthetic batch queue/budget/checkpoint foundation with
the local research_runner/multimodal artifact layout under fail-closed contracts:

1. Input Contract: validates plan_sha256, 77-source ledger mapping (video_id and
   source_ids), run_id, exact model ID, execution mode, active lease token and
   expiry, attempt bounds, frozen budget policy/reservation, and streaming
   SHA-256 + byte size of local input media manifests.
2. Artifact Integrity: stream-hashes real files on disk inside the approved
   per-video root directory, blocks path traversal and symlinks, enforces
   stage-to-stage input/output manifest linkage, verifies the 9-stage hash
   chain, supports idempotent after-commit replay, and rejects modified,
   missing, replaced, or partially completed artifacts. Kept strictly separate
   from Phase 5D synthetic-only checkpoints.
3. AGY Usage Contract: normalizes saved synthetic AGY usage events into the
   three Phase 5C token buckets plus exact_model_id, usage_provenance, call_id
   deduplication key, and API-equivalent USD estimate without double-counting
   cumulative and per-turn usage. Missing, ambiguous, or model-mismatched usage
   never defaults to zero and never releases reserved budget.
4. Error & Recovery Classification: maps all failures to the 8 auditable error
   categories without leaking raw provider output, secret keys, or lease tokens.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import urllib.request

from .batch_budget import (
    BatchBudgetError,
    MODEL_TOKEN_KEYS,
    RATE_ERRORS,
    _format_usd,
    _get_policy,
    _global_state,
    _model_cost,
    _schema as _budget_schema,
    authorize_reservation_locked,
    batch_budget_status,
    configure_batch_budget,
    pause_global_rate_locked,
    reconcile_paused_reservation,
    resume_global_rate_limit,
    settle_reservation_locked,
)
from .batch_checkpoint import (
    GENESIS,
    HEX_SHA,
    InjectedSyntheticCrash,
    SyntheticStageInvalid,
    _digest as _chain_digest,
)
from .batch_plan import BatchPlanBlocked, plan_research_batch
from .batch_queue import (
    BatchQueueError,
    MAX_ATTEMPTS,
    PLAN_PATTERN,
    _connect,
    _now,
    _plan,
    batch_queue_status,
    claim_batch_job,
    initialize_batch_queue,
    recover_batch_job,
)
from .multimodal import MULTIMODAL_STAGES
from .research_state import RUN_ID, VIDEO_ID, read_source_ledger
from .storage import atomic_write, json_text, read_json


WORKER_CONTRACT_SCHEMA_VERSION = 1
VERIFIED_STAGE_TABLE = "batch_verified_stage_artifacts"
USAGE_SETTLEMENT_TABLE = "worker_usage_settlements"
AUDIT_LOG_TABLE = "worker_contract_audit_log"
CALL_SLOT_TABLE = "worker_call_authorizations"

ALLOWED_EXECUTION_MODES = frozenset({
    "OFFLINE_CONTRACT_VALIDATION_ONLY",
    "SYNTHETIC_DRYRUN_ONLY",
})

ALLOWED_CALL_ROLES = frozenset({
    "primary_analysis",
    "argon_selective_verifier",
})

WORKER_ERROR_CATEGORIES = (
    "GLOBAL_RATE_LIMIT",
    "AUTHORIZATION_FAILURE",
    "INPUT_UNAVAILABLE_OR_BLOCKED",
    "ARTIFACT_INTEGRITY_FAILURE",
    "USAGE_UNKNOWN",
    "TRANSIENT_PROVIDER_FAILURE",
    "LEASE_EXPIRED",
    "CONTRACT_VIOLATION",
)

TRANSIENT_PROVIDER_CODES = frozenset({
    "500",
    "502",
    "503",
    "504",
    "503_SERVICE_UNAVAILABLE",
    "PROVIDER_TIMEOUT",
    "TRANSIENT_PROVIDER_ERROR",
    "CONNECTION_RESET",
})

REQUIRED_RUN_FILES = (
    "multimodal_checkpoint.json",
    "frames_manifest.json",
    "tiered_analysis.json",
    "verification.json",
    "final_report.md",
)

SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
CALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(?:bearer\s+[A-Za-z0-9_.-]+)"),
    re.compile(r"(?i)(?:api[_-]?key|secret|password|lease[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bsk-[0-9A-Za-z_-]{16,}\b"),
    re.compile(r"https?://\S+"),
    re.compile(r"\b[0-9a-f]{48}\b"),
)


def sanitize_code(code: Any, default: str = "UNSPECIFIED_ERROR") -> str:
    raw = str(code or "").strip().upper()
    raw = re.sub(r"[^A-Z0-9_]+", "_", raw).strip("_")
    if not raw:
        return default
    return raw[:96]


def sanitize_audit_text(text: Any) -> str:
    """Remove raw provider text, URLs, secret keys, and 48-hex lease tokens."""
    cleaned = str(text or "")
    for pattern in SENSITIVE_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


class WorkerContractError(ValueError):
    """Fail-closed Phase 5E contract error with mandatory category & safe code."""

    def __init__(self, category: str, code: str, detail: Optional[str] = None):
        if category not in WORKER_ERROR_CATEGORIES:
            raise ValueError(f"UNKNOWN_WORKER_ERROR_CATEGORY:{category}")
        self.category = category
        self.code = sanitize_code(code)
        self.detail = sanitize_code(detail) if detail else None
        msg = f"{self.category}:{self.code}"
        if self.detail:
            msg = f"{msg}:{self.detail}"
        super().__init__(msg)


def classify_worker_error(exc: BaseException) -> Dict[str, Any]:
    """Map any Phase 5 or contract exception into the 8 canonical error categories."""
    if isinstance(exc, WorkerContractError):
        cat = exc.category
        code = exc.code
    else:
        raw = str(exc)
        token = sanitize_code(raw.split(":", 1)[0] if ":" in raw else raw)
        if any(k in raw for k in (
            "GLOBAL_RATE_LIMIT", "GLOBAL_RATE_WINDOW", "GLOBAL_RATE_COOLDOWN",
            "GLOBAL_RATE_UNSETTLED", "GLOBAL_RATE_NOT_PAUSED", "429", "RESOURCE_EXHAUSTED",
        )):
            cat, code = "GLOBAL_RATE_LIMIT", token
        elif any(k in raw for k in (
            "LEASE_EXPIRED", "QUEUE_LEASE_EXPIRED", "STAGE_LEASE_EXPIRED",
        )):
            cat, code = "LEASE_EXPIRED", token
        elif any(k in raw for k in (
            "LEASE_TOKEN", "ACTIVE_LEASE_REQUIRED", "LIVE_PLAN_FORBIDDEN",
            "LIVE_MODE_FORBIDDEN", "LIVE_AGY", "AMBIGUOUS_LIVE", "UNAUTHORIZED",
        )):
            cat, code = "AUTHORIZATION_FAILURE", token
        elif any(k in raw for k in (
            "PLAN_STALE", "MEDIA_CACHE", "INPUT_MISSING", "VIDEO_NOT_IN_REGISTERED",
        )):
            cat, code = "INPUT_UNAVAILABLE_OR_BLOCKED", token
        elif any(k in raw for k in (
            "STAGE_", "ARTIFACT_", "CHECKPOINT_",
        )):
            cat, code = "ARTIFACT_INTEGRITY_FAILURE", token
        elif any(k in raw for k in (
            "USAGE_REQUIRED", "UNKNOWN_USAGE", "TOKEN_FIELDS_INVALID",
            "USAGE_WITHOUT_POLICY",
        )):
            cat, code = "USAGE_UNKNOWN", token
        else:
            cat, code = "CONTRACT_VIOLATION", token

    return {
        "error_category": cat,
        "error_code": code,
        "triggers_global_pause": cat == "GLOBAL_RATE_LIMIT",
        "retains_budget_reservation": cat in (
            "GLOBAL_RATE_LIMIT",
            "USAGE_UNKNOWN",
            "LEASE_EXPIRED",
            "ARTIFACT_INTEGRITY_FAILURE",
            "CONTRACT_VIOLATION",
            "AUTHORIZATION_FAILURE",
        ),
        "automatic_retry_allowed": False,
        "raw_provider_logged": False,
    }


def _contract_tables(db: sqlite3.Connection) -> None:
    _budget_schema(db)
    db.executescript(f"""
    CREATE TABLE IF NOT EXISTS {VERIFIED_STAGE_TABLE} (
      plan_sha TEXT NOT NULL,
      video_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      stage_index INTEGER NOT NULL,
      stage_name TEXT NOT NULL,
      input_manifest_json TEXT NOT NULL,
      output_manifest_json TEXT NOT NULL,
      input_manifest_sha256 TEXT NOT NULL,
      output_manifest_sha256 TEXT NOT NULL,
      first_attempt INTEGER NOT NULL,
      recorded_utc TEXT NOT NULL,
      previous_sha256 TEXT NOT NULL,
      event_sha256 TEXT NOT NULL,
      verification_mode TEXT NOT NULL DEFAULT 'OFFLINE_FILE_VERIFIED_CONTRACT',
      live_execution INTEGER NOT NULL DEFAULT 0 CHECK(live_execution = 0),
      PRIMARY KEY(plan_sha, video_id, stage_index),
      UNIQUE(plan_sha, video_id, stage_name)
    );
    CREATE TABLE IF NOT EXISTS {USAGE_SETTLEMENT_TABLE} (
      plan_sha TEXT NOT NULL,
      video_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      attempt INTEGER NOT NULL,
      call_id TEXT NOT NULL,
      call_role TEXT NOT NULL,
      exact_model_id TEXT NOT NULL,
      dedup_key TEXT NOT NULL UNIQUE,
      uncached_input_tokens INTEGER NOT NULL,
      cache_read_tokens INTEGER NOT NULL,
      output_tokens INTEGER NOT NULL,
      estimated_api_equivalent_usd TEXT NOT NULL,
      usage_provenance TEXT NOT NULL,
      recorded_utc TEXT NOT NULL,
      PRIMARY KEY(plan_sha, video_id, attempt, call_id)
    );
    CREATE TABLE IF NOT EXISTS {CALL_SLOT_TABLE} (
      plan_sha TEXT NOT NULL,
      video_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      attempt INTEGER NOT NULL,
      call_index INTEGER NOT NULL,
      call_id TEXT NOT NULL,
      call_role TEXT NOT NULL,
      exact_model_id TEXT NOT NULL,
      reserved_tokens_json TEXT NOT NULL,
      reserved_api_equivalent_usd TEXT NOT NULL,
      created_utc TEXT NOT NULL,
      PRIMARY KEY(plan_sha, video_id, attempt, call_index),
      UNIQUE(plan_sha, video_id, attempt, call_id)
    );
    CREATE TABLE IF NOT EXISTS {AUDIT_LOG_TABLE} (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,
      plan_sha TEXT NOT NULL,
      video_id TEXT NOT NULL,
      run_id TEXT,
      attempt INTEGER,
      from_status TEXT NOT NULL,
      to_status TEXT NOT NULL,
      error_category TEXT,
      error_code TEXT,
      stage_name TEXT,
      recorded_utc TEXT NOT NULL
    );
    """)


def _record_audit_event_locked(
    db: sqlite3.Connection,
    *,
    plan_sha: str,
    video_id: str,
    run_id: Optional[str],
    attempt: Optional[int],
    from_status: str,
    to_status: str,
    error_category: Optional[str] = None,
    error_code: Optional[str] = None,
    stage_name: Optional[str] = None,
    now: datetime,
) -> None:
    if error_category is not None and error_category not in WORKER_ERROR_CATEGORIES:
        raise ValueError(f"INVALID_AUDIT_ERROR_CATEGORY:{error_category}")
    safe_code = sanitize_code(error_code) if error_code else None
    db.execute(
        f"INSERT INTO {AUDIT_LOG_TABLE} "
        "(plan_sha,video_id,run_id,attempt,from_status,to_status,"
        "error_category,error_code,stage_name,recorded_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            plan_sha,
            video_id,
            run_id,
            attempt,
            sanitize_code(from_status, "UNKNOWN"),
            sanitize_code(to_status, "UNKNOWN"),
            error_category,
            safe_code,
            stage_name,
            now.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        ),
    )


def list_worker_audit_events(root: Path, plan_sha: str,
                             video_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with closing(_connect(root)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (AUDIT_LOG_TABLE,),
        ).fetchone()
        if not exists:
            return []
        if video_id:
            rows = db.execute(
                f"SELECT plan_sha,video_id,run_id,attempt,from_status,to_status,"
                f"error_category,error_code,stage_name,recorded_utc "
                f"FROM {AUDIT_LOG_TABLE} WHERE plan_sha=? AND video_id=? ORDER BY event_id",
                (plan_sha, video_id),
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT plan_sha,video_id,run_id,attempt,from_status,to_status,"
                f"error_category,error_code,stage_name,recorded_utc "
                f"FROM {AUDIT_LOG_TABLE} WHERE plan_sha=? ORDER BY event_id",
                (plan_sha,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Streaming File Hashing & Path Traversal Guard (Section [2])
# ---------------------------------------------------------------------------

def streaming_file_sha256(path: Path, *, chunk_size: int = 65536) -> Tuple[str, int]:
    """Stream actual file bytes in chunks and return (sha256_hex, size_bytes)."""
    if chunk_size < 1024:
        raise ValueError("chunk_size must be >= 1024")
    hasher = hashlib.sha256()
    total_bytes = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            total_bytes += len(chunk)
    return hasher.hexdigest(), total_bytes


def verify_approved_artifact_path(
    root: Path,
    video_id: str,
    target_path: Path | str,
) -> Path:
    """Ensure target_path is inside data/videos/<video_id> with no traversal or symlink."""
    if not VIDEO_ID.fullmatch(str(video_id or "")):
        raise WorkerContractError("CONTRACT_VIOLATION", "VIDEO_ID_INVALID")
    if not isinstance(target_path, (str, Path)) or not str(target_path).strip():
        raise WorkerContractError("ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_PATH_EMPTY")
    raw_str = str(target_path)
    if "\x00" in raw_str:
        raise WorkerContractError("ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_PATH_NULL_BYTE")

    raw_path = Path(raw_str)
    if ".." in raw_path.parts:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_PATH_TRAVERSAL_BLOCKED"
        )

    root_resolved = Path(root).resolve()
    approved_video_root = (root_resolved / "data" / "videos" / video_id).resolve()

    if raw_path.is_absolute():
        candidate = raw_path
    else:
        # Support paths relative to workspace root or approved video root.
        if raw_path.parts[:3] == ("data", "videos", video_id):
            candidate = root_resolved / raw_path
        else:
            candidate = approved_video_root / raw_path

    # Reject symlinks anywhere along the path before resolving.
    probe = candidate
    while True:
        if probe.is_symlink():
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_SYMLINK_FORBIDDEN"
            )
        if probe == probe.parent or probe == root_resolved:
            break
        probe = probe.parent

    resolved = candidate.resolve()
    try:
        resolved.relative_to(approved_video_root)
    except ValueError as exc:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_PATH_OUTSIDE_APPROVED_ROOT"
        ) from exc

    if resolved == approved_video_root:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_PATH_MUST_BE_FILE"
        )
    return resolved


def verify_artifact_entry_on_disk(
    root: Path,
    video_id: str,
    entry: Mapping[str, Any],
    *,
    missing_category: str = "ARTIFACT_INTEGRITY_FAILURE",
) -> Dict[str, Any]:
    """Verify existence, approved root confinement, byte size, and streaming SHA-256."""
    if not isinstance(entry, Mapping):
        raise WorkerContractError("ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_ENTRY_NOT_OBJECT")
    raw_path = entry.get("path") or entry.get("cached_path") or entry.get("relpath")
    expected_sha = entry.get("sha256")
    expected_size = entry.get("size_bytes")

    if not isinstance(expected_sha, str) or not HEX_SHA.fullmatch(expected_sha):
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_EXPECTED_SHA256_INVALID"
        )
    if type(expected_size) is not int or expected_size < 0:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_EXPECTED_SIZE_INVALID"
        )

    resolved = verify_approved_artifact_path(root, video_id, str(raw_path or ""))
    if not resolved.exists() or not resolved.is_file() or resolved.is_symlink():
        raise WorkerContractError(missing_category, "ARTIFACT_FILE_MISSING")

    stat_size = resolved.stat().st_size
    actual_sha, streamed_size = streaming_file_sha256(resolved)
    if stat_size != streamed_size or streamed_size != expected_size:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_SIZE_MISMATCH"
        )
    if not secrets.compare_digest(actual_sha, expected_sha):
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_SHA256_MISMATCH"
        )

    root_resolved = Path(root).resolve()
    relpath = resolved.relative_to(root_resolved).as_posix()
    return {
        "name": str(entry.get("name") or resolved.name),
        "relpath": relpath,
        "size_bytes": streamed_size,
        "sha256": actual_sha,
    }


def build_stage_artifact_manifest(
    root: Path,
    video_id: str,
    files: Sequence[Path | str | Mapping[str, Any]],
) -> Dict[str, Any]:
    """Create a canonical manifest from real local files by streaming their bytes."""
    if not files:
        raise WorkerContractError("ARTIFACT_INTEGRITY_FAILURE", "MANIFEST_FILES_EMPTY")
    root_resolved = Path(root).resolve()
    items: List[Dict[str, Any]] = []
    seen_relpaths = set()
    for item in files:
        if isinstance(item, Mapping):
            verified = verify_artifact_entry_on_disk(root_resolved, video_id, item)
        else:
            resolved = verify_approved_artifact_path(root_resolved, video_id, item)
            if not resolved.exists() or not resolved.is_file():
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_FILE_MISSING"
                )
            sha, size = streaming_file_sha256(resolved)
            verified = {
                "name": resolved.name,
                "relpath": resolved.relative_to(root_resolved).as_posix(),
                "size_bytes": size,
                "sha256": sha,
            }
        if verified["relpath"] in seen_relpaths:
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "MANIFEST_DUPLICATE_ARTIFACT_PATH"
            )
        seen_relpaths.add(verified["relpath"])
        items.append(verified)
    return {
        "schema_version": 1,
        "video_id": video_id,
        "artifacts": items,
    }


def _verify_manifest_on_disk(
    root: Path,
    video_id: str,
    manifest: Mapping[str, Any],
    *,
    label: str,
) -> Tuple[Dict[str, Any], str]:
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", f"{label}_MANIFEST_SCHEMA_INVALID"
        )
    if manifest.get("video_id") != video_id:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", f"{label}_MANIFEST_VIDEO_ID_MISMATCH"
        )
    raw_items = manifest.get("artifacts")
    if not isinstance(raw_items, list) or not raw_items:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", f"{label}_MANIFEST_ARTIFACTS_EMPTY"
        )
    normalized_items: List[Dict[str, Any]] = []
    seen = set()
    for entry in raw_items:
        verified = verify_artifact_entry_on_disk(root, video_id, entry)
        if verified["relpath"] in seen:
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", f"{label}_MANIFEST_DUPLICATE_PATH"
            )
        seen.add(verified["relpath"])
        normalized_items.append(verified)

        # If this artifact is frames_manifest.json, also verify referenced
        # frame/ROI/board files on disk via streaming SHA-256.
        if verified["name"] == "frames_manifest.json":
            _verify_frames_manifest_contents_on_disk(
                root, video_id, Path(root).resolve() / verified["relpath"]
            )

    normalized_manifest = {
        "schema_version": 1,
        "video_id": video_id,
        "artifacts": normalized_items,
    }
    manifest_sha = _chain_digest(normalized_manifest)
    return normalized_manifest, manifest_sha


def _verify_frames_manifest_contents_on_disk(
    root: Path,
    video_id: str,
    manifest_path: Path,
) -> None:
    try:
        doc = read_json(manifest_path)
    except Exception as exc:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "FRAMES_MANIFEST_INVALID_JSON"
        ) from exc
    if not isinstance(doc, Mapping):
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "FRAMES_MANIFEST_NOT_OBJECT"
        )
    for key in ("frames", "roi_frames", "boards"):
        collection = doc.get(key, []) or []
        if not isinstance(collection, list):
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "FRAMES_MANIFEST_COLLECTION_INVALID"
            )
        for item in collection:
            if not isinstance(item, Mapping):
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "FRAMES_MANIFEST_ITEM_INVALID"
                )
            p_raw = item.get("path")
            sha_raw = item.get("sha256")
            if not p_raw or not isinstance(sha_raw, str) or not HEX_SHA.fullmatch(sha_raw):
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "FRAMES_MANIFEST_ASSET_HASH_MISSING"
                )
            resolved = verify_approved_artifact_path(root, video_id, str(p_raw))
            if not resolved.is_file():
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "FRAMES_MANIFEST_ASSET_FILE_MISSING"
                )
            actual_sha, _ = streaming_file_sha256(resolved)
            if not secrets.compare_digest(actual_sha, sha_raw):
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "FRAMES_MANIFEST_ASSET_SHA256_MISMATCH"
                )


# ---------------------------------------------------------------------------
# Multi-Call / Argon Selective Verification Contract Design (Section [3])
# ---------------------------------------------------------------------------

def validate_multi_call_authorization_contract(
    frozen_policy: Mapping[str, Any],
    call_slots: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate separate per-call authorization & metering slots (e.g. Flash + Argon).

    Phase 5E validates the contract schema and per-call budget bounds offline;
    it never executes live multi-model calls.
    """
    if not isinstance(call_slots, Sequence) or not call_slots:
        raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOTS_EMPTY")
    if len(call_slots) > 4:
        raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOTS_MAX_EXCEEDED")

    models_policy = frozen_policy.get("models", {})
    global_policy = frozen_policy.get("global", {})
    unit_tokens = int(frozen_policy.get("pricing", {}).get("unit_tokens", 1_000_000))

    seen_call_ids = set()
    seen_roles = set()
    per_model_tokens: Dict[str, int] = {}
    per_model_usd: Dict[str, Decimal] = {}
    normalized_slots: List[Dict[str, Any]] = []

    for expected_idx, slot in enumerate(call_slots, start=1):
        if not isinstance(slot, Mapping):
            raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOT_NOT_OBJECT")
        call_index = slot.get("call_index", expected_idx)
        if type(call_index) is not int or call_index != expected_idx:
            raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOT_INDEX_INVALID")
        call_id = str(slot.get("call_id") or "")
        if not CALL_ID_RE.fullmatch(call_id) or call_id in seen_call_ids:
            raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOT_ID_INVALID_OR_DUPLICATE")
        seen_call_ids.add(call_id)

        role = str(slot.get("call_role") or "")
        if role not in ALLOWED_CALL_ROLES:
            raise WorkerContractError("AUTHORIZATION_FAILURE", "CALL_SLOT_ROLE_UNAUTHORIZED")
        if expected_idx == 1 and role != "primary_analysis":
            raise WorkerContractError("CONTRACT_VIOLATION", "PRIMARY_CALL_SLOT_MUST_BE_FIRST")
        if role in seen_roles:
            raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOT_DUPLICATE_ROLE")
        seen_roles.add(role)

        model_id = str(slot.get("exact_model_id") or "")
        if model_id not in models_policy:
            raise WorkerContractError("AUTHORIZATION_FAILURE", "CALL_SLOT_MODEL_UNAUTHORIZED")

        if slot.get("allow_live_execution") is True:
            raise WorkerContractError("AUTHORIZATION_FAILURE", "CALL_SLOT_LIVE_EXECUTION_FORBIDDEN")

        reserve_raw = slot.get("reserved_tokens")
        if not isinstance(reserve_raw, Mapping) or set(reserve_raw) != set(MODEL_TOKEN_KEYS):
            raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOT_RESERVE_TOKENS_INVALID")
        reserve_tokens: Dict[str, int] = {}
        for k in MODEL_TOKEN_KEYS:
            val = reserve_raw[k]
            if type(val) is not int or val < 0:
                raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOT_RESERVE_COUNT_INVALID")
            reserve_tokens[k] = val
        slot_total_tokens = sum(reserve_tokens.values())
        if slot_total_tokens <= 0:
            raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOT_EMPTY_RESERVATION")

        rates = models_policy[model_id]["rates_per_unit_usd"]
        slot_cost = _model_cost(reserve_tokens, rates, unit_tokens)
        slot_cost_usd = Decimal(_format_usd(slot_cost))

        per_model_tokens[model_id] = per_model_tokens.get(model_id, 0) + slot_total_tokens
        per_model_usd[model_id] = per_model_usd.get(model_id, Decimal("0")) + slot_cost_usd

        if (
            per_model_tokens[model_id] > int(models_policy[model_id]["max_tokens"])
            or per_model_usd[model_id] > Decimal(str(models_policy[model_id]["max_api_equivalent_usd"]))
        ):
            raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOT_EXCEEDS_MODEL_CAP")

        normalized_slots.append({
            "call_index": call_index,
            "call_id": call_id,
            "call_role": role,
            "exact_model_id": model_id,
            "reserved_tokens": reserve_tokens,
            "reserved_total_tokens": slot_total_tokens,
            "reserved_api_equivalent_usd": str(slot_cost_usd),
            "requires_separate_metering": True,
            "live_execution_authorized": False,
        })

    total_tokens = sum(per_model_tokens.values())
    total_usd = sum(per_model_usd.values(), Decimal("0"))
    if (
        total_tokens > int(global_policy["max_tokens"])
        or total_usd > Decimal(str(global_policy["max_api_equivalent_usd"]))
    ):
        raise WorkerContractError("CONTRACT_VIOLATION", "CALL_SLOTS_EXCEED_GLOBAL_CAP")

    return {
        "slot_count": len(normalized_slots),
        "slots": normalized_slots,
        "total_reserved_tokens": total_tokens,
        "total_reserved_api_equivalent_usd": _format_usd(total_usd),
        "multi_model_live_execution_enabled": False,
    }


# ---------------------------------------------------------------------------
# Input Contract Builder & Validator (Section [1])
# ---------------------------------------------------------------------------


def _assert_worker_lease_locked(
    db: sqlite3.Connection,
    contract: Mapping[str, Any],
    now: datetime,
) -> None:
    """Fence mutations with current lease, reservation and global pause in one transaction."""
    if _global_state(db)["paused"]:
        raise WorkerContractError("GLOBAL_RATE_LIMIT", "GLOBAL_RATE_LIMIT_PAUSED")
    plan_sha = str(contract.get("plan_sha256") or "")
    video_id = str(contract.get("video_id") or "")
    job = db.execute(
        "SELECT status,attempts,lease_token,owner,lease_expires_utc "
        "FROM queue_jobs WHERE plan_sha=? AND video_id=?",
        (plan_sha, video_id),
    ).fetchone()
    if job is None or job["status"] != "LEASED":
        raise WorkerContractError("AUTHORIZATION_FAILURE", "QUEUE_JOB_NOT_LEASED")
    token = contract.get("lease_token")
    if (not isinstance(token, str) or not token
            or not secrets.compare_digest(str(job["lease_token"] or ""), token)):
        raise WorkerContractError("AUTHORIZATION_FAILURE", "LEASE_TOKEN_INVALID")
    if (not isinstance(contract.get("owner"), str)
            or not contract["owner"].strip()
            or contract["owner"] != job["owner"]):
        raise WorkerContractError("AUTHORIZATION_FAILURE", "LEASE_OWNER_MISMATCH")
    if type(contract.get("attempt")) is not int or contract["attempt"] != job["attempts"]:
        raise WorkerContractError("CONTRACT_VIOLATION", "ATTEMPT_COUNT_MISMATCH")
    try:
        expiry = datetime.fromisoformat(str(job["lease_expires_utc"]))
        if expiry.tzinfo is None:
            raise ValueError("missing timezone")
    except (ValueError, TypeError) as exc:
        raise WorkerContractError(
            "CONTRACT_VIOLATION", "LEASE_EXPIRY_FORMAT_INVALID"
        ) from exc
    if expiry <= now:
        raise WorkerContractError("LEASE_EXPIRED", "LEASE_EXPIRED_RECOVERY_REQUIRED")
    if str(contract.get("lease_expires_utc") or "") != str(job["lease_expires_utc"]):
        raise WorkerContractError("CONTRACT_VIOLATION", "LEASE_EXPIRY_MISMATCH")
    policy = _get_policy(db, plan_sha)
    if policy is None:
        raise WorkerContractError("CONTRACT_VIOLATION", "BUDGET_POLICY_NOT_CONFIGURED")
    policy_meta = db.execute(
        "SELECT budget_id,policy_sha256 FROM budget_policies WHERE plan_sha=?",
        (plan_sha,),
    ).fetchone()
    meta = contract.get("budget_policy")
    if (policy_meta is None or not isinstance(meta, Mapping)
            or meta.get("budget_id") != policy_meta["budget_id"]
            or meta.get("policy_sha256") != policy_meta["policy_sha256"]):
        raise WorkerContractError("CONTRACT_VIOLATION", "CONTRACT_BUDGET_POLICY_MISMATCH")
    model = str(contract.get("exact_model_id") or "")
    if model not in policy["models"]:
        raise WorkerContractError("CONTRACT_VIOLATION", "MODEL_NOT_IN_BUDGET_POLICY")
    reservation = db.execute(
        "SELECT model,status FROM budget_reservations "
        "WHERE plan_sha=? AND video_id=? AND attempt=?",
        (plan_sha, video_id, job["attempts"]),
    ).fetchone()
    if reservation is None or reservation["status"] != "RESERVED":
        raise WorkerContractError(
            "CONTRACT_VIOLATION", "ACTIVE_BUDGET_RESERVATION_REQUIRED"
        )
    if reservation["model"] != model:
        raise WorkerContractError("CONTRACT_VIOLATION", "RESERVED_MODEL_MISMATCH")


def build_worker_input_contract(
    root: Path,
    *,
    plan_sha256: str,
    video_id: str,
    run_id: str,
    exact_model_id: str,
    lease_token: str,
    lease_expires_utc: str,
    attempt: int,
    owner: Optional[str] = None,
    execution_mode: str = "OFFLINE_CONTRACT_VALIDATION_ONLY",
    call_slots: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Construct a worker input contract from saved plan, budget policy, and local media cache."""
    root_resolved = Path(root).resolve()
    plan_file = root_resolved / "data" / "batch_plans" / f"batch_plan_{plan_sha256[:16]}.json"
    plan_doc = read_json(plan_file) if plan_file.is_file() else {}
    job_row = next(
        (r for r in plan_doc.get("jobs", []) if r.get("video_id") == video_id),
        {},
    )
    source_ids = list(job_row.get("source_ids", []))

    with closing(_connect(root_resolved)) as db:
        policy = _get_policy(db, plan_sha256)
        policy_row = db.execute(
            "SELECT budget_id, policy_sha256 FROM budget_policies WHERE plan_sha=?",
            (plan_sha256,),
        ).fetchone() if policy else None
        lease_row = db.execute(
            "SELECT owner FROM queue_jobs WHERE plan_sha=? AND video_id=?",
            (plan_sha256, video_id),
        ).fetchone()
    resolved_owner = owner if owner is not None else (lease_row["owner"] if lease_row else None)

    cache_path = root_resolved / "data" / "videos" / video_id / "media_cache.json"
    if not cache_path.is_file():
        raise WorkerContractError("INPUT_UNAVAILABLE_OR_BLOCKED", "MEDIA_CACHE_MISSING")
    cache_doc = read_json(cache_path)
    assets_raw = cache_doc.get("assets", {}) if isinstance(cache_doc, Mapping) else {}
    input_assets: Dict[str, Any] = {}
    for asset_key in ("video", "subtitle_json3", "highres_video", "audio"):
        row = assets_raw.get(asset_key)
        if not isinstance(row, Mapping) or not row.get("cached_path"):
            if asset_key in ("video", "subtitle_json3"):
                raise WorkerContractError(
                    "INPUT_UNAVAILABLE_OR_BLOCKED", f"MEDIA_INPUT_MISSING_{asset_key.upper()}"
                )
            continue
        resolved = verify_approved_artifact_path(root_resolved, video_id, str(row["cached_path"]))
        if not resolved.is_file():
            raise WorkerContractError(
                "INPUT_UNAVAILABLE_OR_BLOCKED", f"MEDIA_FILE_MISSING_{asset_key.upper()}"
            )
        sha, size = streaming_file_sha256(resolved)
        input_assets[asset_key] = {
            "name": resolved.name,
            "relpath": resolved.relative_to(root_resolved).as_posix(),
            "size_bytes": size,
            "sha256": sha,
        }

    cache_sha, cache_size = streaming_file_sha256(cache_path)
    contract: Dict[str, Any] = {
        "schema_version": WORKER_CONTRACT_SCHEMA_VERSION,
        "plan_sha256": plan_sha256,
        "video_id": video_id,
        "source_ids": source_ids,
        "run_id": run_id,
        "exact_model_id": exact_model_id,
        "execution_mode": execution_mode,
        "allow_live_agy": False,
        "allow_external_network": False,
        "owner": resolved_owner,
        "lease_token": lease_token,
        "lease_expires_utc": lease_expires_utc,
        "attempt": attempt,
        "budget_policy": {
            "budget_id": policy_row["budget_id"] if policy_row else "",
            "policy_sha256": policy_row["policy_sha256"] if policy_row else "",
        },
        "input_manifest": {
            "schema_version": 1,
            "video_id": video_id,
            "media_cache_relpath": cache_path.relative_to(root_resolved).as_posix(),
            "media_cache_size_bytes": cache_size,
            "media_cache_sha256": cache_sha,
            "assets": input_assets,
        },
    }
    if call_slots is not None:
        contract["call_slots"] = list(call_slots)
    return contract


def validate_worker_input_contract(
    root: Path,
    contract: Mapping[str, Any],
    *,
    now_utc: Optional[datetime] = None,
    check_stale_plan: bool = True,
) -> Dict[str, Any]:
    """Validate all 9 mandatory worker input contract targets fail-closed.

    Passing validation NEVER authorizes live AGY calls or external network I/O.
    """
    if not isinstance(contract, Mapping) or contract.get("schema_version") != WORKER_CONTRACT_SCHEMA_VERSION:
        raise WorkerContractError("CONTRACT_VIOLATION", "CONTRACT_SCHEMA_VERSION_INVALID")

    # 1. Execution mode & authorization flags check first (fail-closed on live opt-in)
    mode = contract.get("execution_mode")
    if mode not in ALLOWED_EXECUTION_MODES:
        raise WorkerContractError("AUTHORIZATION_FAILURE", "EXECUTION_MODE_NOT_PERMITTED")
    if contract.get("allow_live_agy") is not False or contract.get("allow_external_network") is not False:
        raise WorkerContractError("AUTHORIZATION_FAILURE", "LIVE_EXECUTION_FLAGS_FORBIDDEN")

    # 2. Plan SHA-256 and stale plan check
    root_resolved = Path(root).resolve()
    plan_sha = str(contract.get("plan_sha256") or "")
    if not PLAN_PATTERN.fullmatch(plan_sha):
        raise WorkerContractError("CONTRACT_VIOLATION", "PLAN_SHA256_FORMAT_INVALID")
    try:
        plan = _plan(root_resolved, plan_sha, check_stale=check_stale_plan)
    except BatchQueueError as exc:
        code = str(exc)
        if "STALE" in code:
            raise WorkerContractError("INPUT_UNAVAILABLE_OR_BLOCKED", code) from exc
        if "LIVE_PLAN_FORBIDDEN" in code:
            raise WorkerContractError("AUTHORIZATION_FAILURE", code) from exc
        raise WorkerContractError("CONTRACT_VIOLATION", code) from exc
    except (BatchPlanBlocked, ValueError, FileNotFoundError) as exc:
        raise WorkerContractError(
            "INPUT_UNAVAILABLE_OR_BLOCKED", "SOURCE_LEDGER_OR_PLAN_INVALID"
        ) from exc

    # 3. video_id, 77-source ledger integrity, and source_ids mapping
    video_id = str(contract.get("video_id") or "")
    if not VIDEO_ID.fullmatch(video_id):
        raise WorkerContractError("CONTRACT_VIOLATION", "VIDEO_ID_FORMAT_INVALID")
    try:
        ledger = read_source_ledger(root_resolved, expected=int(plan.get("source_entries", 77)))
    except Exception as exc:
        raise WorkerContractError(
            "INPUT_UNAVAILABLE_OR_BLOCKED", "SOURCE_LEDGER_UNAVAILABLE"
        ) from exc

    ledger_source_ids = [
        entry["source_id"] for entry in ledger["entries"] if entry["video_id"] == video_id
    ]
    if not ledger_source_ids:
        raise WorkerContractError(
            "INPUT_UNAVAILABLE_OR_BLOCKED", "VIDEO_NOT_IN_77_SOURCE_LEDGER"
        )
    contract_source_ids = contract.get("source_ids")
    if (
        not isinstance(contract_source_ids, list)
        or not contract_source_ids
        or list(contract_source_ids) != ledger_source_ids
    ):
        raise WorkerContractError("CONTRACT_VIOLATION", "SOURCE_IDS_LEDGER_MISMATCH")

    plan_job = next((r for r in plan.get("jobs", []) if r.get("video_id") == video_id), None)
    if (
        plan_job is None
        or plan_job.get("disposition") != "PLAN_ONLY"
        or video_id not in plan.get("plan_only_video_ids", [])
        or list(plan_job.get("source_ids", [])) != ledger_source_ids
    ):
        raise WorkerContractError("CONTRACT_VIOLATION", "VIDEO_NOT_PLANNED_IN_BATCH_PLAN")

    # 4. run_id format
    run_id = str(contract.get("run_id") or "")
    if not RUN_ID.fullmatch(run_id):
        raise WorkerContractError("CONTRACT_VIOLATION", "RUN_ID_FORMAT_INVALID")

    # 5. Attempt bounds
    attempt = contract.get("attempt")
    if type(attempt) is not int or not 1 <= attempt <= MAX_ATTEMPTS:
        raise WorkerContractError("CONTRACT_VIOLATION", "ATTEMPT_OUT_OF_RANGE")

    # 6. Exact model ID
    exact_model_id = str(contract.get("exact_model_id") or "")
    if not exact_model_id or exact_model_id in ("auto", "flash", "argon", "UNKNOWN"):
        raise WorkerContractError("CONTRACT_VIOLATION", "EXACT_MODEL_ID_REQUIRED")

    now = _now(now_utc)

    # 7. Queue lease validity & 8. Budget policy + reservation check
    with closing(_connect(root_resolved)) as db:
        _contract_tables(db)
        rate_state = _global_state(db)
        if rate_state["paused"]:
            raise WorkerContractError("GLOBAL_RATE_LIMIT", "GLOBAL_RATE_LIMIT_PAUSED")

        job_row = db.execute(
            "SELECT status,attempts,lease_token,owner,lease_expires_utc "
            "FROM queue_jobs WHERE plan_sha=? AND video_id=?",
            (plan_sha, video_id),
        ).fetchone()
        if job_row is None:
            raise WorkerContractError("CONTRACT_VIOLATION", "QUEUE_JOB_NOT_FOUND")
        if job_row["status"] != "LEASED":
            raise WorkerContractError("AUTHORIZATION_FAILURE", "QUEUE_JOB_NOT_LEASED")

        supplied_token = contract.get("lease_token")
        if (
            not isinstance(supplied_token, str)
            or not supplied_token
            or not secrets.compare_digest(str(job_row["lease_token"] or ""), supplied_token)
        ):
            raise WorkerContractError("AUTHORIZATION_FAILURE", "LEASE_TOKEN_INVALID")

        if (not isinstance(contract.get("owner"), str)
                or not contract["owner"].strip()
                or contract["owner"] != job_row["owner"]):
            raise WorkerContractError("AUTHORIZATION_FAILURE", "LEASE_OWNER_MISMATCH")

        supplied_expires = str(contract.get("lease_expires_utc") or "")
        if supplied_expires != str(job_row["lease_expires_utc"] or ""):
            raise WorkerContractError("CONTRACT_VIOLATION", "LEASE_EXPIRY_MISMATCH")
        try:
            expires_dt = datetime.fromisoformat(str(job_row["lease_expires_utc"]))
            if expires_dt.tzinfo is None:
                raise ValueError("tz")
        except ValueError as exc:
            raise WorkerContractError("CONTRACT_VIOLATION", "LEASE_EXPIRY_FORMAT_INVALID") from exc
        if expires_dt <= now:
            raise WorkerContractError("LEASE_EXPIRED", "LEASE_EXPIRED_RECOVERY_REQUIRED")

        if int(job_row["attempts"]) != attempt:
            raise WorkerContractError("CONTRACT_VIOLATION", "ATTEMPT_COUNT_MISMATCH")

        policy = _get_policy(db, plan_sha)
        if policy is None:
            raise WorkerContractError("CONTRACT_VIOLATION", "BUDGET_POLICY_NOT_CONFIGURED")
        policy_meta = db.execute(
            "SELECT budget_id, policy_sha256 FROM budget_policies WHERE plan_sha=?",
            (plan_sha,),
        ).fetchone()
        contract_budget = contract.get("budget_policy")
        if not isinstance(contract_budget, Mapping):
            raise WorkerContractError("CONTRACT_VIOLATION", "CONTRACT_BUDGET_POLICY_MISSING")
        if (
            contract_budget.get("budget_id") != policy_meta["budget_id"]
            or contract_budget.get("policy_sha256") != policy_meta["policy_sha256"]
        ):
            raise WorkerContractError("CONTRACT_VIOLATION", "CONTRACT_BUDGET_POLICY_MISMATCH")

        if exact_model_id not in policy.get("models", {}):
            raise WorkerContractError("CONTRACT_VIOLATION", "MODEL_NOT_IN_BUDGET_POLICY")

        reservation = db.execute(
            "SELECT model,status,reserved_tokens_json,reserved_cost_usd "
            "FROM budget_reservations WHERE plan_sha=? AND video_id=? AND attempt=?",
            (plan_sha, video_id, attempt),
        ).fetchone()
        if reservation is None or reservation["status"] != "RESERVED":
            raise WorkerContractError("CONTRACT_VIOLATION", "ACTIVE_BUDGET_RESERVATION_REQUIRED")
        if reservation["model"] != exact_model_id:
            raise WorkerContractError("CONTRACT_VIOLATION", "RESERVED_MODEL_MISMATCH")

        call_slot_summary = None
        if "call_slots" in contract and contract["call_slots"] is not None:
            call_slot_summary = validate_multi_call_authorization_contract(
                policy, contract["call_slots"]
            )

    # 9. Input Manifest verification with streaming SHA-256 on disk
    input_manifest = contract.get("input_manifest")
    if not isinstance(input_manifest, Mapping) or input_manifest.get("schema_version") != 1:
        raise WorkerContractError("CONTRACT_VIOLATION", "INPUT_MANIFEST_SCHEMA_INVALID")
    if input_manifest.get("video_id") != video_id:
        raise WorkerContractError("CONTRACT_VIOLATION", "INPUT_MANIFEST_VIDEO_ID_MISMATCH")

    cache_rel = input_manifest.get("media_cache_relpath")
    cache_sha = input_manifest.get("media_cache_sha256")
    cache_size = input_manifest.get("media_cache_size_bytes")
    verified_cache = verify_artifact_entry_on_disk(
        root_resolved,
        video_id,
        {
            "name": "media_cache.json",
            "relpath": cache_rel,
            "sha256": cache_sha,
            "size_bytes": cache_size,
        },
        missing_category="INPUT_UNAVAILABLE_OR_BLOCKED",
    )

    assets = input_manifest.get("assets")
    if not isinstance(assets, Mapping):
        raise WorkerContractError("CONTRACT_VIOLATION", "INPUT_MANIFEST_ASSETS_INVALID")
    for req_key in ("video", "subtitle_json3"):
        if req_key not in assets:
            raise WorkerContractError(
                "INPUT_UNAVAILABLE_OR_BLOCKED", f"INPUT_MANIFEST_MISSING_{req_key.upper()}"
            )

    verified_assets: Dict[str, Any] = {}
    for key, item in assets.items():
        if key not in ("video", "subtitle_json3", "highres_video", "audio"):
            raise WorkerContractError("CONTRACT_VIOLATION", "INPUT_MANIFEST_UNKNOWN_ASSET_KEY")
        verified_assets[key] = verify_artifact_entry_on_disk(
            root_resolved,
            video_id,
            item,
            missing_category="INPUT_UNAVAILABLE_OR_BLOCKED",
        )

    # Never include lease_token in validation output.
    return {
        "contract_valid": True,
        "schema_version": WORKER_CONTRACT_SCHEMA_VERSION,
        "execution_mode": mode,
        "plan_sha256": plan_sha,
        "video_id": video_id,
        "source_ids": ledger_source_ids,
        "run_id": run_id,
        "exact_model_id": exact_model_id,
        "attempt": attempt,
        "budget_id": policy_meta["budget_id"],
        "policy_sha256": policy_meta["policy_sha256"],
        "verified_media_cache": verified_cache,
        "verified_input_assets": verified_assets,
        "call_slot_authorization": call_slot_summary,
        "live_model_execution_permitted": False,
        "external_network_allowed": False,
    }


# ---------------------------------------------------------------------------
# Verified File Stage Checkpoints (Section [2])
# ---------------------------------------------------------------------------

def _read_verified_stage_chain_locked(
    db: sqlite3.Connection,
    root: Path,
    plan_sha: str,
    video_id: str,
    *,
    verify_files_on_disk: bool = True,
) -> List[Dict[str, Any]]:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (VERIFIED_STAGE_TABLE,),
    ).fetchone()
    if not exists:
        return []
    rows = db.execute(
        f"SELECT stage_index,stage_name,run_id,input_manifest_json,"
        f"output_manifest_json,input_manifest_sha256,output_manifest_sha256,"
        f"first_attempt,recorded_utc,previous_sha256,event_sha256,"
        f"verification_mode,live_execution "
        f"FROM {VERIFIED_STAGE_TABLE} WHERE plan_sha=? AND video_id=? "
        f"ORDER BY stage_index",
        (plan_sha, video_id),
    ).fetchall()

    prev = GENESIS
    events: List[Dict[str, Any]] = []
    chain_run_id: Optional[str] = None
    for expected_index, row in enumerate(rows):
        event = dict(row)
        if chain_run_id is None:
            chain_run_id = event["run_id"]
        elif event["run_id"] != chain_run_id:
            raise WorkerContractError("CONTRACT_VIOLATION", "RUN_ID_CHAIN_MISMATCH")
        if (
            event["stage_index"] != expected_index
            or expected_index >= len(MULTIMODAL_STAGES)
            or event["stage_name"] != MULTIMODAL_STAGES[expected_index]
        ):
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "STAGE_SEQUENCE_GAP_OR_REORDERED"
            )
        if (
            event["verification_mode"] != "OFFLINE_FILE_VERIFIED_CONTRACT"
            or event["live_execution"] != 0
        ):
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "STAGE_VERIFICATION_MODE_TAMPERED"
            )
        try:
            in_manifest = json.loads(event["input_manifest_json"])
            out_manifest = json.loads(event["output_manifest_json"])
        except Exception as exc:
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "STAGE_STORED_MANIFEST_CORRUPTED"
            ) from exc

        if (
            _chain_digest(in_manifest) != event["input_manifest_sha256"]
            or _chain_digest(out_manifest) != event["output_manifest_sha256"]
        ):
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "STAGE_MANIFEST_DIGEST_TAMPERED"
            )

        body = {
            "stage_index": event["stage_index"],
            "stage_name": event["stage_name"],
            "run_id": event["run_id"],
            "input_manifest_sha256": event["input_manifest_sha256"],
            "output_manifest_sha256": event["output_manifest_sha256"],
            "first_attempt": event["first_attempt"],
            "recorded_utc": event["recorded_utc"],
            "previous_sha256": event["previous_sha256"],
        }
        actual_event_sha = event["event_sha256"]
        if event["previous_sha256"] != prev or not secrets.compare_digest(
            str(actual_event_sha), _chain_digest(body)
        ):
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "STAGE_CHECKPOINT_CHAIN_CORRUPTED"
            )

        if verify_files_on_disk:
            _, on_disk_in_sha = _verify_manifest_on_disk(
                root, video_id, in_manifest, label="STAGE_INPUT"
            )
            _, on_disk_out_sha = _verify_manifest_on_disk(
                root, video_id, out_manifest, label="STAGE_OUTPUT"
            )
            if (
                on_disk_in_sha != event["input_manifest_sha256"]
                or on_disk_out_sha != event["output_manifest_sha256"]
            ):
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_MODIFIED_ON_DISK"
                )

        event["input_manifest"] = in_manifest
        event["output_manifest"] = out_manifest
        events.append(event)
        prev = actual_event_sha
    return events


def _verify_stage_manifest_linkage(
    stage_index: int,
    contract_validation: Mapping[str, Any],
    prior_events: Sequence[Mapping[str, Any]],
    in_manifest: Mapping[str, Any],
    out_manifest: Mapping[str, Any],
) -> None:
    in_by_rel = {a["relpath"]: a for a in in_manifest["artifacts"]}
    out_by_rel = {a["relpath"]: a for a in out_manifest["artifacts"]}

    if stage_index == 0:
        # Stage 0 (media_cached) input must link to the verified contract input assets.
        for req_key in ("video", "subtitle_json3"):
            asset = contract_validation["verified_input_assets"][req_key]
            linked = in_by_rel.get(asset["relpath"])
            if (
                linked is None
                or linked["sha256"] != asset["sha256"]
                or linked["size_bytes"] != asset["size_bytes"]
            ):
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "STAGE0_INPUT_MANIFEST_UNLINKED"
                )
        if not any(a["name"] == "media_cache.json" for a in out_manifest["artifacts"]):
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "STAGE0_OUTPUT_MEDIA_CACHE_REQUIRED"
            )
    else:
        # Stage i > 0 input must include at least one output artifact from stage i-1
        # with identical relpath, size_bytes, and sha256, and any artifact matching
        # an earlier stage output path must match its committed sha256.
        prev_out = prior_events[stage_index - 1]["output_manifest"]["artifacts"]
        if not any(
            in_by_rel.get(p["relpath"]) == p
            for p in prev_out
        ):
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "STAGE_INPUT_OUTPUT_LINKAGE_BROKEN"
            )
        committed_outputs: Dict[str, Dict[str, Any]] = {}
        for ev in prior_events:
            for art in ev["output_manifest"]["artifacts"]:
                committed_outputs[art["relpath"]] = art
        for rel, art in in_by_rel.items():
            if rel in committed_outputs and committed_outputs[rel] != art:
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "STAGE_UPSTREAM_ARTIFACT_HASH_MISMATCH"
                )

    if not out_by_rel:
        raise WorkerContractError("ARTIFACT_INTEGRITY_FAILURE", "STAGE_OUTPUT_EMPTY")


def record_verified_stage_artifact(
    root: Path,
    contract: Mapping[str, Any],
    *,
    stage_name: str,
    input_manifest: Mapping[str, Any],
    output_manifest: Mapping[str, Any],
    now_utc: Optional[datetime] = None,
    inject_fault: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify real on-disk stage input/output files and append to the verified chain."""
    if inject_fault not in (None, "before_insert", "after_insert", "after_commit"):
        raise WorkerContractError("CONTRACT_VIOLATION", "STAGE_FAULT_POINT_INVALID")
    if stage_name not in MULTIMODAL_STAGES:
        raise WorkerContractError("ARTIFACT_INTEGRITY_FAILURE", "STAGE_UNKNOWN_NAME")

    root_resolved = Path(root).resolve()
    now = _now(now_utc)
    validated = validate_worker_input_contract(
        root_resolved, contract, now_utc=now, check_stale_plan=False
    )
    plan_sha = validated["plan_sha256"]
    video_id = validated["video_id"]
    run_id = validated["run_id"]
    attempt = validated["attempt"]

    norm_in, in_sha = _verify_manifest_on_disk(
        root_resolved, video_id, input_manifest, label="STAGE_INPUT"
    )
    norm_out, out_sha = _verify_manifest_on_disk(
        root_resolved, video_id, output_manifest, label="STAGE_OUTPUT"
    )

    replayed = False
    stage_index = MULTIMODAL_STAGES.index(stage_name)

    with closing(_connect(root_resolved)) as db:
        _contract_tables(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            _assert_worker_lease_locked(db, contract, now)
            _, current_in_sha = _verify_manifest_on_disk(
                root_resolved, video_id, norm_in, label="STAGE_INPUT"
            )
            _, current_out_sha = _verify_manifest_on_disk(
                root_resolved, video_id, norm_out, label="STAGE_OUTPUT"
            )
            if current_in_sha != in_sha or current_out_sha != out_sha:
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "ARTIFACT_CHANGED_BEFORE_STAGE_COMMIT"
                )
            prior = _read_verified_stage_chain_locked(
                db, root_resolved, plan_sha, video_id, verify_files_on_disk=True
            )
            if prior and prior[0]["run_id"] != run_id:
                raise WorkerContractError("CONTRACT_VIOLATION", "RUN_ID_CHAIN_MISMATCH")
            if stage_index < len(prior):
                earlier = prior[stage_index]
                if (
                    earlier["run_id"] != run_id
                    or earlier["input_manifest_sha256"] != in_sha
                    or earlier["output_manifest_sha256"] != out_sha
                ):
                    raise WorkerContractError(
                        "ARTIFACT_INTEGRITY_FAILURE", "STAGE_REPLAY_HASH_MISMATCH"
                    )
                replayed = True
            elif stage_index != len(prior):
                raise WorkerContractError(
                    "ARTIFACT_INTEGRITY_FAILURE", "STAGE_OUT_OF_ORDER"
                )
            else:
                _verify_stage_manifest_linkage(
                    stage_index, validated, prior, norm_in, norm_out
                )
                if inject_fault == "before_insert":
                    raise InjectedSyntheticCrash("INJECTED_BEFORE_VERIFIED_STAGE_INSERT")
                body = {
                    "stage_index": stage_index,
                    "stage_name": stage_name,
                    "run_id": run_id,
                    "input_manifest_sha256": in_sha,
                    "output_manifest_sha256": out_sha,
                    "first_attempt": attempt,
                    "recorded_utc": now.isoformat(timespec="microseconds"),
                    "previous_sha256": prior[-1]["event_sha256"] if prior else GENESIS,
                }
                event_sha = _chain_digest(body)
                db.execute(
                    f"INSERT INTO {VERIFIED_STAGE_TABLE} "
                    "(plan_sha,video_id,run_id,stage_index,stage_name,"
                    "input_manifest_json,output_manifest_json,"
                    "input_manifest_sha256,output_manifest_sha256,"
                    "first_attempt,recorded_utc,previous_sha256,event_sha256,"
                    "verification_mode,live_execution) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'OFFLINE_FILE_VERIFIED_CONTRACT',0)",
                    (
                        plan_sha,
                        video_id,
                        run_id,
                        stage_index,
                        stage_name,
                        json_text(norm_in),
                        json_text(norm_out),
                        in_sha,
                        out_sha,
                        attempt,
                        body["recorded_utc"],
                        body["previous_sha256"],
                        event_sha,
                    ),
                )
                if inject_fault == "after_insert":
                    raise InjectedSyntheticCrash("INJECTED_AFTER_VERIFIED_STAGE_INSERT")
            db.commit()
        except Exception:
            db.rollback()
            raise

    if inject_fault == "after_commit" and not replayed:
        raise InjectedSyntheticCrash("INJECTED_AFTER_VERIFIED_STAGE_COMMIT")

    status = verified_stage_status(root_resolved, plan_sha, video_id)
    status["replayed_existing_checkpoint"] = replayed
    return status


def verified_stage_status(
    root: Path,
    plan_sha: str,
    video_id: str,
    *,
    verify_files_on_disk: bool = True,
) -> Dict[str, Any]:
    """Inspect verified stage chain and re-verify on-disk artifacts; never exposes lease token."""
    root_resolved = Path(root).resolve()
    _plan(root_resolved, plan_sha, check_stale=False)
    with closing(_connect(root_resolved)) as db:
        job = db.execute(
            "SELECT status,attempts FROM queue_jobs WHERE plan_sha=? AND video_id=?",
            (plan_sha, video_id),
        ).fetchone()
        if job is None:
            raise WorkerContractError("CONTRACT_VIOLATION", "QUEUE_JOB_NOT_FOUND")
        events = _read_verified_stage_chain_locked(
            db, root_resolved, plan_sha, video_id, verify_files_on_disk=verify_files_on_disk
        )
    count = len(events)
    return {
        "mode": "OFFLINE_FILE_VERIFIED_CONTRACT",
        "plan_sha256": plan_sha,
        "video_id": video_id,
        "run_id": events[0]["run_id"] if events else None,
        "job_status": job["status"],
        "stages_done": count,
        "stages_total": len(MULTIMODAL_STAGES),
        "next_stage": MULTIMODAL_STAGES[count] if count < len(MULTIMODAL_STAGES) else None,
        "verified_stage_complete": count == len(MULTIMODAL_STAGES),
        "real_artifact_contents_verified": bool(count > 0 and verify_files_on_disk),
        "production_live_analysis_completed": False,
        "stages": [
            {
                "stage_index": e["stage_index"],
                "stage_name": e["stage_name"],
                "input_manifest_sha256": e["input_manifest_sha256"],
                "output_manifest_sha256": e["output_manifest_sha256"],
                "first_attempt": e["first_attempt"],
                "event_sha256": e["event_sha256"],
            }
            for e in events
        ],
    }


def _verify_completed_run_artifacts_on_disk(
    root: Path,
    video_id: str,
    run_id: str,
    events: Sequence[Mapping[str, Any]],
) -> None:
    """Reject partial or tampered run outputs even if multimodal_checkpoint.json claims COMPLETED."""
    if len(events) != len(MULTIMODAL_STAGES):
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "PARTIAL_STAGE_COMPLETION_REJECTED"
        )
    run_dir = verify_approved_artifact_path(
        root, video_id, Path("data") / "videos" / video_id / "runs" / run_id / "multimodal_checkpoint.json"
    ).parent

    for filename in REQUIRED_RUN_FILES:
        file_path = verify_approved_artifact_path(root, video_id, run_dir / filename)
        if not file_path.is_file():
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", f"REQUIRED_RUN_ARTIFACT_MISSING_{filename.upper()}"
            )
        _, size = streaming_file_sha256(file_path)
        if size <= 0:
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", f"REQUIRED_RUN_ARTIFACT_EMPTY_{filename.upper()}"
            )

    ckpt_path = run_dir / "multimodal_checkpoint.json"
    try:
        ckpt = read_json(ckpt_path)
    except Exception as exc:
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "RUN_CHECKPOINT_INVALID_JSON"
        ) from exc
    if (
        not isinstance(ckpt, Mapping)
        or ckpt.get("video_id") != video_id
        or ckpt.get("run_id") != run_id
        or ckpt.get("status") != "COMPLETED"
    ):
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "RUN_CHECKPOINT_NOT_COMPLETED_OR_MISMATCHED"
        )
    stages_map = ckpt.get("stages")
    if not isinstance(stages_map, Mapping):
        raise WorkerContractError(
            "ARTIFACT_INTEGRITY_FAILURE", "RUN_CHECKPOINT_STAGES_MISSING"
        )
    for stage_name in MULTIMODAL_STAGES:
        entry = stages_map.get(stage_name)
        if not isinstance(entry, Mapping) or entry.get("status") != "DONE":
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "RUN_CHECKPOINT_STAGE_INCOMPLETE"
            )
        art_path = entry.get("artifact_path")
        if not art_path:
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "RUN_CHECKPOINT_STAGE_ARTIFACT_PATH_MISSING"
            )
        resolved_art = verify_approved_artifact_path(root, video_id, str(art_path))
        if not resolved_art.is_file():
            raise WorkerContractError(
                "ARTIFACT_INTEGRITY_FAILURE", "RUN_CHECKPOINT_STAGE_ARTIFACT_MISSING"
            )

    _verify_frames_manifest_contents_on_disk(
        root, video_id, run_dir / "frames_manifest.json"
    )


def assert_verified_stage_completion_if_started_locked(
    db: sqlite3.Connection,
    root: Path,
    plan_sha: str,
    video_id: str,
) -> None:
    """Hook for batch_queue.finish_batch_job: if verified stages exist, all 9 + run files must pass."""
    events = _read_verified_stage_chain_locked(
        db, Path(root).resolve(), plan_sha, video_id, verify_files_on_disk=True
    )
    if events:
        _verify_completed_run_artifacts_on_disk(
            Path(root).resolve(), video_id, events[0]["run_id"], events
        )


# ---------------------------------------------------------------------------
# AGY Usage Normalization & Settlement Contract (Section [3])
# ---------------------------------------------------------------------------

def _extract_strict_three_tokens(raw: Mapping[str, Any]) -> Dict[str, int]:
    """Require explicit non-negative integer counts for the 3 token fields; never default to 0."""
    if not isinstance(raw, Mapping):
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_RECORD_NOT_OBJECT")

    has_uncached = "uncached_input_tokens" in raw
    has_input = "input_tokens" in raw
    if not has_uncached and not has_input:
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_INPUT_TOKENS_MISSING")
    if has_uncached and has_input and raw["uncached_input_tokens"] != raw["input_tokens"]:
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_INPUT_TOKENS_CONFLICT")

    uncached_val = raw["uncached_input_tokens"] if has_uncached else raw["input_tokens"]
    if "cache_read_tokens" not in raw or "output_tokens" not in raw:
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_REQUIRED_TOKEN_BUCKET_MISSING")

    values = {
        "uncached_input_tokens": uncached_val,
        "cache_read_tokens": raw["cache_read_tokens"],
        "output_tokens": raw["output_tokens"],
    }
    cleaned: Dict[str, int] = {}
    for key, val in values.items():
        if type(val) is not int or val < 0:
            raise WorkerContractError("USAGE_UNKNOWN", f"USAGE_INVALID_TOKEN_COUNT_{key.upper()}")
        cleaned[key] = val
    return cleaned


def normalize_agy_usage_contract(
    usage_source: str | Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    expected_model_id: str,
    plan_sha256: str,
    video_id: str,
    run_id: str,
    attempt: int,
    call_id: str = "primary-call-1",
    call_role: str = "primary_analysis",
    frozen_policy: Optional[Mapping[str, Any]] = None,
    provenance: str = "SYNTHETIC_SAVED_EVENT_NORMALIZED",
) -> Dict[str, Any]:
    """Normalize saved synthetic AGY usage without external calls or cumulative double-counting."""
    if usage_source is None:
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_MISSING")
    if not expected_model_id or expected_model_id in ("auto", "UNKNOWN"):
        raise WorkerContractError("CONTRACT_VIOLATION", "EXPECTED_EXACT_MODEL_ID_REQUIRED")
    if not CALL_ID_RE.fullmatch(str(call_id or "")):
        raise WorkerContractError("CONTRACT_VIOLATION", "USAGE_CALL_ID_INVALID")
    if call_role not in ALLOWED_CALL_ROLES:
        raise WorkerContractError("AUTHORIZATION_FAILURE", "USAGE_CALL_ROLE_UNAUTHORIZED")

    events_list: Optional[List[Mapping[str, Any]]] = None
    if isinstance(usage_source, str):
        if not usage_source.strip():
            raise WorkerContractError("USAGE_UNKNOWN", "USAGE_STREAM_EMPTY")
        parsed_lines: List[Mapping[str, Any]] = []
        for line in usage_source.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except Exception as exc:
                raise WorkerContractError(
                    "USAGE_UNKNOWN", "USAGE_STREAM_JSON_MALFORMED"
                ) from exc
            if not isinstance(obj, Mapping):
                raise WorkerContractError("USAGE_UNKNOWN", "USAGE_STREAM_EVENT_NOT_OBJECT")
            parsed_lines.append(obj)
        events_list = parsed_lines
    elif isinstance(usage_source, Sequence) and not isinstance(usage_source, (bytes, bytearray)):
        events_list = list(usage_source)

    observed_model: Optional[str] = None
    turn_usages: List[Dict[str, int]] = []
    cumulative_usage: Optional[Dict[str, int]] = None

    if events_list is not None:
        if not events_list:
            raise WorkerContractError("USAGE_UNKNOWN", "USAGE_EVENTS_EMPTY")
        seen_step_ids = set()
        result_seen = False
        for ev in events_list:
            if not isinstance(ev, Mapping):
                raise WorkerContractError("USAGE_UNKNOWN", "USAGE_EVENT_NOT_OBJECT")
            ev_type = ev.get("event")
            if ev_type == "init":
                init_obj = ev.get("init")
                if not isinstance(init_obj, Mapping) or not init_obj.get("model"):
                    raise WorkerContractError("USAGE_UNKNOWN", "USAGE_INIT_MODEL_MISSING")
                m_val = str(init_obj["model"])
                if observed_model and observed_model != m_val:
                    raise WorkerContractError("USAGE_UNKNOWN", "USAGE_CONFLICTING_INIT_MODELS")
                observed_model = m_val
            elif ev_type == "step_update":
                su = ev.get("step_update")
                if not isinstance(su, Mapping):
                    raise WorkerContractError("USAGE_UNKNOWN", "USAGE_STEP_UPDATE_INVALID")
                if su.get("state") == "DONE" and "usage" in su and su["usage"] is not None:
                    step_id = su.get("step_id") or su.get("turn_id") or su.get("step_index")
                    if step_id is not None:
                        if step_id in seen_step_ids:
                            raise WorkerContractError(
                                "USAGE_UNKNOWN", "USAGE_DUPLICATE_STEP_ID"
                            )
                        seen_step_ids.add(step_id)
                    if su.get("usage_scope") == "cumulative" or su.get("is_cumulative") is True:
                        raise WorkerContractError(
                            "USAGE_UNKNOWN", "USAGE_CUMULATIVE_IN_STEP_UPDATE_REJECTED"
                        )
                    turn_usages.append(_extract_strict_three_tokens(su["usage"]))
            elif ev_type == "result":
                if result_seen:
                    raise WorkerContractError("USAGE_UNKNOWN", "USAGE_DUPLICATE_RESULT_EVENT")
                result_seen = True
                res = ev.get("result")
                if not isinstance(res, Mapping) or res.get("status") not in (None, "SUCCESS"):
                    raise WorkerContractError("USAGE_UNKNOWN", "USAGE_TERMINAL_RESULT_NOT_SUCCESS")
                if res.get("model"):
                    m_val = str(res["model"])
                    if observed_model and observed_model != m_val:
                        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_MODEL_MISMATCH_IN_RESULT")
                    observed_model = m_val
                if "usage" in res and res["usage"] is not None:
                    cumulative_usage = _extract_strict_three_tokens(res["usage"])
    elif isinstance(usage_source, Mapping):
        if not usage_source:
            raise WorkerContractError("USAGE_UNKNOWN", "USAGE_OBJECT_EMPTY")
        if usage_source.get("double_counted") is True or usage_source.get("include_cumulative_with_turns") is True:
            raise WorkerContractError("USAGE_UNKNOWN", "USAGE_DUPLICATE_CUMULATIVE_REJECTED")
        raw_model = (
            usage_source.get("exact_model_id")
            or usage_source.get("init_model")
            or usage_source.get("model")
        )
        observed_model = str(raw_model) if raw_model else expected_model_id

        if "records" in usage_source:
            recs = usage_source["records"]
            if not isinstance(recs, list) or not recs:
                raise WorkerContractError("USAGE_UNKNOWN", "USAGE_RECORDS_INVALID")
            seen_ids = set()
            has_turn = False
            has_cum = False
            for r in recs:
                if not isinstance(r, Mapping):
                    raise WorkerContractError("USAGE_UNKNOWN", "USAGE_RECORD_ITEM_INVALID")
                rid = r.get("record_id") or r.get("turn_id") or r.get("call_id")
                if rid is not None:
                    if rid in seen_ids:
                        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_DUPLICATE_RECORD_ID")
                    seen_ids.add(rid)
                scope = str(r.get("scope") or r.get("kind") or "turn").lower()
                if scope == "cumulative" or r.get("is_cumulative") is True:
                    if has_cum:
                        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_DUPLICATE_CUMULATIVE_REJECTED")
                    has_cum = True
                    cumulative_usage = _extract_strict_three_tokens(r)
                else:
                    has_turn = True
                    turn_usages.append(_extract_strict_three_tokens(r))
            if has_turn and has_cum:
                # If caller also provided a top-level sum that added cumulative + turns,
                # or mixed cumulative into a turn list without matching, reject.
                turn_sum = {
                    k: sum(t[k] for t in turn_usages) for k in MODEL_TOKEN_KEYS
                }
                if cumulative_usage != turn_sum or usage_source.get("sum_all_records") is True:
                    raise WorkerContractError(
                        "USAGE_UNKNOWN", "USAGE_DUPLICATE_CUMULATIVE_REJECTED"
                    )
        else:
            if "turn_usages" in usage_source or "turns" in usage_source:
                raw_turns = usage_source.get("turn_usages") or usage_source.get("turns")
                if not isinstance(raw_turns, list) or not raw_turns:
                    raise WorkerContractError("USAGE_UNKNOWN", "USAGE_TURNS_EMPTY")
                seen_turn_ids = set()
                for t in raw_turns:
                    if not isinstance(t, Mapping):
                        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_TURN_NOT_OBJECT")
                    tid = t.get("turn_id") or t.get("turn_number")
                    if tid is not None:
                        if tid in seen_turn_ids:
                            raise WorkerContractError("USAGE_UNKNOWN", "USAGE_DUPLICATE_TURN_ID")
                        seen_turn_ids.add(tid)
                    turn_usages.append(_extract_strict_three_tokens(t))
            if "cumulative_usage" in usage_source and usage_source["cumulative_usage"] is not None:
                cumulative_usage = _extract_strict_three_tokens(usage_source["cumulative_usage"])
            if not turn_usages and cumulative_usage is None:
                cumulative_usage = _extract_strict_three_tokens(usage_source)
            elif turn_usages and any(k in usage_source for k in MODEL_TOKEN_KEYS):
                top_tokens = _extract_strict_three_tokens(usage_source)
                if cumulative_usage is not None and top_tokens != cumulative_usage:
                    raise WorkerContractError(
                        "USAGE_UNKNOWN", "USAGE_TOP_AND_CUMULATIVE_CONFLICT"
                    )
                cumulative_usage = top_tokens
    else:
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_SOURCE_TYPE_UNSUPPORTED")

    if not observed_model or observed_model == "UNKNOWN":
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_MODEL_ID_UNKNOWN")
    if observed_model != expected_model_id:
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_EXACT_MODEL_ID_MISMATCH")

    if turn_usages and cumulative_usage is not None:
        summed_turns = {
            k: sum(item[k] for item in turn_usages) for k in MODEL_TOKEN_KEYS
        }
        if summed_turns != cumulative_usage:
            raise WorkerContractError(
                "USAGE_UNKNOWN", "USAGE_DUPLICATE_CUMULATIVE_OR_TURN_MISMATCH"
            )
        final_tokens = summed_turns
    elif turn_usages:
        final_tokens = {
            k: sum(item[k] for item in turn_usages) for k in MODEL_TOKEN_KEYS
        }
    elif cumulative_usage is not None:
        final_tokens = dict(cumulative_usage)
    else:
        raise WorkerContractError("USAGE_UNKNOWN", "USAGE_NO_TOKEN_DATA_EXTRACTED")

    estimated_usd_str = "UNKNOWN"
    if frozen_policy is not None:
        models_map = frozen_policy.get("models", {})
        if observed_model not in models_map:
            raise WorkerContractError("CONTRACT_VIOLATION", "USAGE_MODEL_NOT_IN_POLICY")
        rates = models_map[observed_model]["rates_per_unit_usd"]
        unit = int(frozen_policy.get("pricing", {}).get("unit_tokens", 1_000_000))
        cost_dec = _model_cost(final_tokens, rates, unit)
        estimated_usd_str = _format_usd(cost_dec)

    dedup_payload = {
        "plan_sha256": plan_sha256,
        "video_id": video_id,
        "run_id": run_id,
        "attempt": attempt,
        "call_id": call_id,
        "call_role": call_role,
        "exact_model_id": observed_model,
    }
    dedup_key = _chain_digest(dedup_payload)

    return {
        "exact_model_id": observed_model,
        "uncached_input_tokens": final_tokens["uncached_input_tokens"],
        "cache_read_tokens": final_tokens["cache_read_tokens"],
        "output_tokens": final_tokens["output_tokens"],
        "total_tokens": sum(final_tokens.values()),
        "phase5c_tokens": dict(final_tokens),
        "usage_provenance": provenance,
        "call_id": call_id,
        "call_role": call_role,
        "dedup_key": dedup_key,
        "turn_count": len(turn_usages) if turn_usages else 1,
        "double_counting_prevented": True,
        "estimated_api_equivalent_usd": estimated_usd_str,
        "billing_interpretation": "API_EQUIVALENT_ESTIMATE_NOT_AGY_BILL",
        "actual_agy_billing_usd": None,
        "actual_agy_invoice_observed": False,
        "usage_status": "NORMALIZED_COMPLETE",
    }


# ---------------------------------------------------------------------------
# Worker Contract Job Completion & Failure Transitions (Section [1]-[4])
# ---------------------------------------------------------------------------

def finish_worker_contract_job(
    root: Path,
    contract: Mapping[str, Any],
    *,
    success: bool,
    usage_source: Any = None,
    call_id: str = "primary-call-1",
    call_role: str = "primary_analysis",
    provider_error_code: Optional[str] = None,
    retry_after_seconds: int = 0,
    now_utc: Optional[datetime] = None,
    require_phase5f_receipt: bool = False,
) -> Dict[str, Any]:
    """Complete or pause a leased worker contract job transactionally.

    - On success=True: requires all 9 verified-file stage checkpoints AND
      verified run files on disk AND normalized complete usage within the
      reserved budget. Marks queue status SIMULATED (never LIVE).
    - On success=False: classifies rate limit (429 -> global pause) vs
      transient provider failure (job-local pause), retains unsettled budget
      reservation whenever usage is unknown, and logs sanitized audit state.
    """
    if type(require_phase5f_receipt) is not bool:
        raise WorkerContractError("CONTRACT_VIOLATION", "RECEIPT_REQUIREMENT_INVALID")
    if require_phase5f_receipt and not success:
        raise WorkerContractError("CONTRACT_VIOLATION", "RECEIPT_FOR_SUCCESS_ONLY")
    root_resolved = Path(root).resolve()
    now = _now(now_utc)
    validated = validate_worker_input_contract(
        root_resolved, contract, now_utc=now, check_stale_plan=False
    )
    plan_sha = validated["plan_sha256"]
    video_id = validated["video_id"]
    run_id = validated["run_id"]
    attempt = validated["attempt"]
    exact_model_id = validated["exact_model_id"]

    with closing(_connect(root_resolved)) as db:
        _contract_tables(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            _assert_worker_lease_locked(db, contract, now)
            policy = _get_policy(db, plan_sha)
            if policy is None:
                raise WorkerContractError("CONTRACT_VIOLATION", "BUDGET_POLICY_NOT_CONFIGURED")

            if success:
                if provider_error_code is not None:
                    raise WorkerContractError(
                        "CONTRACT_VIOLATION", "PROVIDER_ERROR_CODE_FORBIDDEN_ON_SUCCESS"
                    )
                events = _read_verified_stage_chain_locked(
                    db, root_resolved, plan_sha, video_id, verify_files_on_disk=True
                )
                if events and events[0]["run_id"] != run_id:
                    raise WorkerContractError("CONTRACT_VIOLATION", "RUN_ID_CHAIN_MISMATCH")
                _verify_completed_run_artifacts_on_disk(
                    root_resolved, video_id, run_id, events
                )
                if require_phase5f_receipt:
                    # Verify immutable receipt under THIS SAME SQLite transaction,
                    # before usage settlement or the final fenced queue update.
                    from .phase5f_staging import verify_phase5f_receipt_locked
                    verify_phase5f_receipt_locked(
                        db, root_resolved, contract, verify_sources=True
                    )
                normalized_usage = normalize_agy_usage_contract(
                    usage_source,
                    expected_model_id=exact_model_id,
                    plan_sha256=plan_sha,
                    video_id=video_id,
                    run_id=run_id,
                    attempt=attempt,
                    call_id=call_id,
                    call_role=call_role,
                    frozen_policy=policy,
                )
                dup = db.execute(
                    f"SELECT 1 FROM {USAGE_SETTLEMENT_TABLE} WHERE dedup_key=?",
                    (normalized_usage["dedup_key"],),
                ).fetchone()
                if dup is not None:
                    raise WorkerContractError(
                        "USAGE_UNKNOWN", "USAGE_DUPLICATE_CALL_ID_ALREADY_SETTLED"
                    )
                try:
                    budget_accounting = settle_reservation_locked(
                        db,
                        plan_sha,
                        video_id,
                        attempt,
                        normalized_usage["phase5c_tokens"],
                        now,
                        provenance=normalized_usage["usage_provenance"],
                    )
                except BatchBudgetError as exc:
                    code = str(exc)
                    if "EXCEEDS_RESERVATION" in code:
                        raise WorkerContractError("CONTRACT_VIOLATION", code) from exc
                    raise WorkerContractError("USAGE_UNKNOWN", code) from exc

                db.execute(
                    f"INSERT INTO {USAGE_SETTLEMENT_TABLE} "
                    "(plan_sha,video_id,run_id,attempt,call_id,call_role,"
                    "exact_model_id,dedup_key,uncached_input_tokens,"
                    "cache_read_tokens,output_tokens,estimated_api_equivalent_usd,"
                    "usage_provenance,recorded_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_sha,
                        video_id,
                        run_id,
                        attempt,
                        normalized_usage["call_id"],
                        normalized_usage["call_role"],
                        normalized_usage["exact_model_id"],
                        normalized_usage["dedup_key"],
                        normalized_usage["uncached_input_tokens"],
                        normalized_usage["cache_read_tokens"],
                        normalized_usage["output_tokens"],
                        normalized_usage["estimated_api_equivalent_usd"],
                        normalized_usage["usage_provenance"],
                        now.isoformat(timespec="microseconds"),
                    ),
                )
                changed = db.execute(
                    "UPDATE queue_jobs SET status='SIMULATED',lease_token=NULL,"
                    "owner=NULL,lease_expires_utc=NULL,updated_utc=? "
                    "WHERE plan_sha=? AND video_id=? AND status='LEASED' "
                    "AND lease_token=? AND owner=? AND attempts=? AND lease_expires_utc=?",
                    (now.isoformat(), plan_sha, video_id, contract["lease_token"],
                        contract["owner"], attempt, contract["lease_expires_utc"]),
                )
                if changed.rowcount != 1:
                    raise WorkerContractError(
                        "AUTHORIZATION_FAILURE", "LEASE_FENCED_UPDATE_REJECTED"
                    )
                _record_audit_event_locked(
                    db,
                    plan_sha=plan_sha,
                    video_id=video_id,
                    run_id=run_id,
                    attempt=attempt,
                    from_status="LEASED",
                    to_status="SIMULATED",
                    now=now,
                )
                db.commit()
                return {
                    "mode": "OFFLINE_CONTRACT_VALIDATION_ONLY",
                    "video_id": video_id,
                    "run_id": run_id,
                    "status": "SIMULATED",
                    "normalized_usage": normalized_usage,
                    "budget_accounting": budget_accounting,
                    "global_rate_paused": False,
                    "live_agy_enabled": False,
                }

            # Failure path (success=False)
            err_code = sanitize_code(provider_error_code or "WORKER_EXECUTION_FAILED")
            if err_code in RATE_ERRORS:
                err_category = "GLOBAL_RATE_LIMIT"
            elif err_code in TRANSIENT_PROVIDER_CODES:
                err_category = "TRANSIENT_PROVIDER_FAILURE"
            else:
                err_category = "CONTRACT_VIOLATION"

            normalized_usage = None
            budget_accounting = None
            if usage_source is not None:
                normalized_usage = normalize_agy_usage_contract(
                    usage_source,
                    expected_model_id=exact_model_id,
                    plan_sha256=plan_sha,
                    video_id=video_id,
                    run_id=run_id,
                    attempt=attempt,
                    call_id=call_id,
                    call_role=call_role,
                    frozen_policy=policy,
                )
                budget_accounting = settle_reservation_locked(
                    db,
                    plan_sha,
                    video_id,
                    attempt,
                    normalized_usage["phase5c_tokens"],
                    now,
                    provenance=normalized_usage["usage_provenance"],
                )

            if err_category == "GLOBAL_RATE_LIMIT":
                pause_global_rate_locked(
                    db,
                    error_code=err_code,
                    actor="contract-worker",
                    reason="Sanitized rate-limit signal recorded by offline worker contract.",
                    retry_after_seconds=retry_after_seconds,
                    now=now,
                )

            changed = db.execute(
                "UPDATE queue_jobs SET status='PAUSED_FAILURE',lease_token=NULL,"
                "owner=NULL,lease_expires_utc=NULL,updated_utc=? "
                "WHERE plan_sha=? AND video_id=? AND status='LEASED' "
                "AND lease_token=? AND owner=? AND attempts=? AND lease_expires_utc=?",
                (now.isoformat(), plan_sha, video_id, contract["lease_token"],
                    contract["owner"], attempt, contract["lease_expires_utc"]),
            )
            if changed.rowcount != 1:
                raise WorkerContractError(
                    "AUTHORIZATION_FAILURE", "LEASE_FENCED_UPDATE_REJECTED"
                )
            _record_audit_event_locked(
                db,
                plan_sha=plan_sha,
                video_id=video_id,
                run_id=run_id,
                attempt=attempt,
                from_status="LEASED",
                to_status="PAUSED_FAILURE",
                error_category=err_category,
                error_code=err_code,
                now=now,
            )
            db.commit()
            return {
                "mode": "OFFLINE_CONTRACT_VALIDATION_ONLY",
                "video_id": video_id,
                "run_id": run_id,
                "status": "PAUSED_FAILURE",
                "error_category": err_category,
                "error_code": err_code,
                "budget_accounting": budget_accounting,
                "reservation_retained": budget_accounting is None,
                "global_rate_paused": err_category == "GLOBAL_RATE_LIMIT",
                "live_agy_enabled": False,
            }
        except Exception:
            db.rollback()
            raise


# ---------------------------------------------------------------------------
# Offline External Call Guard (Section [5] & [6])
# ---------------------------------------------------------------------------

class OfflineExternalCallTracker:
    def __init__(self) -> None:
        self.socket_calls = 0
        self.subprocess_calls = 0
        self.live_agy_calls = 0
        self.youtube_calls = 0
        self.blocked_unauthorized_attempts = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "socket_calls": self.socket_calls,
            "subprocess_calls": self.subprocess_calls,
            "live_agy_calls": self.live_agy_calls,
            "youtube_calls": self.youtube_calls,
            "blocked_unauthorized_attempts": self.blocked_unauthorized_attempts,
        }


@contextmanager
def offline_external_call_guard():
    """Block socket, subprocess, live AGY CLI, and YouTube network access fail-closed."""
    from . import agent as agent_mod
    from . import collector as collector_mod

    tracker = OfflineExternalCallTracker()

    orig_socket_connect = socket.socket.connect
    orig_socket_connect_ex = socket.socket.connect_ex
    orig_create_connection = socket.create_connection
    orig_urlopen = urllib.request.urlopen
    orig_sub_run = subprocess.run
    orig_sub_popen = subprocess.Popen
    orig_sub_call = subprocess.call
    orig_sub_check_call = subprocess.check_call
    orig_sub_check_output = subprocess.check_output
    orig_agy_invoke = agent_mod.Agy._invoke
    orig_fetch_yt = getattr(collector_mod, "fetch_youtube_transcript", None)

    def _block_socket(*_a, **_kw):
        tracker.blocked_unauthorized_attempts += 1
        raise WorkerContractError(
            "AUTHORIZATION_FAILURE", "UNAUTHORIZED_EXTERNAL_SOCKET_CALL"
        )

    def _block_subprocess(*_a, **_kw):
        tracker.blocked_unauthorized_attempts += 1
        raise WorkerContractError(
            "AUTHORIZATION_FAILURE", "UNAUTHORIZED_EXTERNAL_SUBPROCESS_CALL"
        )

    def _block_agy(*_a, **_kw):
        tracker.blocked_unauthorized_attempts += 1
        raise WorkerContractError(
            "AUTHORIZATION_FAILURE", "UNAUTHORIZED_LIVE_AGY_CALL"
        )

    def _block_youtube(*_a, **_kw):
        tracker.blocked_unauthorized_attempts += 1
        raise WorkerContractError(
            "AUTHORIZATION_FAILURE", "UNAUTHORIZED_EXTERNAL_YOUTUBE_CALL"
        )

    socket.socket.connect = _block_socket  # type: ignore[assignment]
    socket.socket.connect_ex = _block_socket  # type: ignore[assignment]
    socket.create_connection = _block_socket  # type: ignore[assignment]
    urllib.request.urlopen = _block_socket  # type: ignore[assignment]
    subprocess.run = _block_subprocess  # type: ignore[assignment]
    subprocess.Popen = _block_subprocess  # type: ignore[assignment]
    subprocess.call = _block_subprocess  # type: ignore[assignment]
    subprocess.check_call = _block_subprocess  # type: ignore[assignment]
    subprocess.check_output = _block_subprocess  # type: ignore[assignment]
    agent_mod.Agy._invoke = _block_agy  # type: ignore[assignment]
    if orig_fetch_yt is not None:
        collector_mod.fetch_youtube_transcript = _block_youtube  # type: ignore[assignment]

    try:
        yield tracker
    finally:
        socket.socket.connect = orig_socket_connect  # type: ignore[assignment]
        socket.socket.connect_ex = orig_socket_connect_ex  # type: ignore[assignment]
        socket.create_connection = orig_create_connection  # type: ignore[assignment]
        urllib.request.urlopen = orig_urlopen  # type: ignore[assignment]
        subprocess.run = orig_sub_run  # type: ignore[assignment]
        subprocess.Popen = orig_sub_popen  # type: ignore[assignment]
        subprocess.call = orig_sub_call  # type: ignore[assignment]
        subprocess.check_call = orig_sub_check_call  # type: ignore[assignment]
        subprocess.check_output = orig_sub_check_output  # type: ignore[assignment]
        agent_mod.Agy._invoke = orig_agy_invoke  # type: ignore[assignment]
        if orig_fetch_yt is not None:
            collector_mod.fetch_youtube_transcript = orig_fetch_yt  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Isolated Test Fixture & E01-E16 Validation Suite Runner (Section [5] & [7])
# ---------------------------------------------------------------------------

DEFAULT_FLASH_MODEL = "gemini-3.8-flash-medium"
DEFAULT_ARGON_MODEL = "gemini-4-argon-high"


def create_phase5e_isolated_workspace(root: Path) -> Dict[str, Any]:
    """Create a self-contained 77-source temporary workspace with local fixture files."""
    root_resolved = Path(root).resolve()
    (root_resolved / "research").mkdir(parents=True, exist_ok=True)
    index_path = root_resolved / "research" / "index.json"
    index_doc = {
        "source_count": 77,
        "source_entries_count": 77,
        "sources": [
            {
                "source_id": f"S{i:03d}",
                "source_ids": [f"S{i:03d}"],
                "video_id": f"{i:011d}",
                "playlist_position": i,
                "status": "not_started",
            }
            for i in range(1, 78)
        ],
    }
    atomic_write(index_path, json_text(index_doc))
    index_bytes = index_path.read_bytes()

    for n in (1, 2, 3):
        vid = f"{n:011d}"
        vdir = root_resolved / "data" / "videos" / vid
        media_dir = vdir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        mp4 = media_dir / "video.mp4"
        json3 = media_dir / "captions.json3"
        mp4.write_bytes(f"isolated-offline-video-bytes-{vid}".encode("utf-8"))
        json3.write_text(
            json.dumps({
                "events": [
                    {"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "오프라인 계약 테스트 자막 1"}]},
                    {"tStartMs": 2000, "dDurationMs": 1500, "segs": [{"utf8": "오프라인 계약 테스트 자막 2"}]},
                ]
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        v_sha, v_size = streaming_file_sha256(mp4)
        s_sha, s_size = streaming_file_sha256(json3)
        cache_doc = {
            "schema_version": 1,
            "video_id": vid,
            "assets": {
                "video": {
                    "kind": "video",
                    "filename": mp4.name,
                    "cached_path": str(mp4),
                    "size_bytes": v_size,
                    "sha256": v_sha,
                },
                "subtitle_json3": {
                    "kind": "subtitle_json3",
                    "filename": json3.name,
                    "cached_path": str(json3),
                    "size_bytes": s_size,
                    "sha256": s_sha,
                },
            },
        }
        atomic_write(vdir / "media_cache.json", json_text(cache_doc))

    plan = plan_research_batch(root_resolved, expected=77, max_jobs=3, save=True)
    plan_sha = plan["plan_sha256"]
    t0 = datetime(2026, 11, 11, 2, 0, 0, tzinfo=timezone.utc)
    initialize_batch_queue(root_resolved, plan_sha, now_utc=t0)

    policy = {
        "schema_version": 1,
        "mode": "SYNTHETIC_DRYRUN_ONLY",
        "budget_id": "phase5e-contract-guard",
        "global": {
            "max_tokens": 120000,
            "max_api_equivalent_usd": "0.600000",
        },
        "models": {
            DEFAULT_FLASH_MODEL: {
                "max_tokens": 80000,
                "max_api_equivalent_usd": "0.250000",
                "reserve": {
                    "uncached_input_tokens": 10000,
                    "cache_read_tokens": 2000,
                    "output_tokens": 2000,
                },
            },
            DEFAULT_ARGON_MODEL: {
                "max_tokens": 40000,
                "max_api_equivalent_usd": "0.350000",
                "reserve": {
                    "uncached_input_tokens": 5000,
                    "cache_read_tokens": 1000,
                    "output_tokens": 1000,
                },
            },
        },
        "rate_limit": {
            "max_requests_per_window": 4,
            "window_seconds": 60,
        },
    }
    policy_path = root_resolved / "phase5e_budget_policy.json"
    atomic_write(policy_path, json_text(policy))
    configure_batch_budget(root_resolved, plan_sha, policy_path, now_utc=t0)
    return {
        "root": root_resolved,
        "plan_sha256": plan_sha,
        "index_bytes": index_bytes,
        "t0": t0,
    }


def materialize_and_record_nine_stages(
    root: Path,
    contract: Mapping[str, Any],
    *,
    now_utc: datetime,
    up_to_stage: int = len(MULTIMODAL_STAGES),
) -> List[Path]:
    """Write deterministic local stage files for a leased video/run and record verified stages."""
    root_resolved = Path(root).resolve()
    video_id = str(contract["video_id"])
    run_id = str(contract["run_id"])
    vdir = root_resolved / "data" / "videos" / video_id
    run_dir = vdir / "runs" / run_id
    (run_dir / "frames").mkdir(parents=True, exist_ok=True)
    (run_dir / "boards").mkdir(parents=True, exist_ok=True)

    media_cache_file = vdir / "media_cache.json"
    video_file = vdir / "media" / "video.mp4"
    sub_file = vdir / "media" / "captions.json3"

    parsed_sub_path = vdir / "parsed_subtitles.json"
    atomic_write(parsed_sub_path, json_text({
        "schema_version": 1,
        "video_id": video_id,
        "valid_segment_count": 2,
        "segments": [
            {"id": "s000001", "segment_id": "s000001", "start": 0.0, "end": 1.5, "timestamp": "00:00", "text": "오프라인 계약 테스트 자막 1"},
            {"id": "s000002", "segment_id": "s000002", "start": 2.0, "end": 3.5, "timestamp": "00:02", "text": "오프라인 계약 테스트 자막 2"},
        ],
    }))

    meta_path = vdir / "media_metadata.json"
    atomic_write(meta_path, json_text({
        "video_id": video_id,
        "probe_backend": "offline_fixture",
        "duration_sec": 4.0,
        "streams": [{"index": 0, "type": "video", "width": 1280, "height": 720}],
    }))

    scenes_path = vdir / "scenes_full_v3.json"
    atomic_write(scenes_path, json_text({
        "video_id": video_id,
        "scene_count": 1,
        "scenes": [{"scene_id": 1, "start_sec": 0.0, "end_sec": 4.0, "keyframe_sec": 1.0, "ui_density_score": 0.1, "is_ui_dense": True}],
    }))

    frame_png = run_dir / "frames" / "frame_001_0001.00s.png"
    board_png = run_dir / "boards" / "board_01.png"
    frame_png.write_bytes(b"\x89PNG\r\n\x1a\noffline-frame-fixture")
    board_png.write_bytes(b"\x89PNG\r\n\x1a\noffline-board-fixture")
    frame_sha, _ = streaming_file_sha256(frame_png)
    board_sha, _ = streaming_file_sha256(board_png)

    frames_manifest_path = run_dir / "frames_manifest.json"
    atomic_write(frames_manifest_path, json_text({
        "video_id": video_id,
        "frame_count": 1,
        "frames": [{
            "frame_id": "frame_001",
            "requested_sec": 1.0,
            "actual_sec": 1.0,
            "width": 1280,
            "height": 720,
            "source_width": 1280,
            "source_height": 720,
            "path": frame_png.relative_to(root_resolved).as_posix(),
            "sha256": frame_sha,
        }],
        "roi_frame_count": 0,
        "roi_frames": [],
        "board_count": 1,
        "boards": [{
            "board_id": "board_01",
            "path": board_png.relative_to(root_resolved).as_posix(),
            "panel_count": 1,
            "panel_frame_ids": ["frame_001"],
            "panel_indices": [1],
            "panel_refs": [{"panel_index": 1, "frame_id": "frame_001", "timestamp_sec": 1.0}],
            "sha256": board_sha,
        }],
    }))

    aligned_path = run_dir / "aligned_timeline.json"
    atomic_write(aligned_path, json_text({
        "video_id": video_id,
        "total_scenes": 1,
        "units": [{"scene_id": 1, "start_sec": 0.0, "end_sec": 4.0, "segment_ids": ["s000001", "s000002"]}],
    }))

    analysis_path = run_dir / "tiered_analysis.json"
    atomic_write(analysis_path, json_text({
        "video_id": video_id,
        "run_id": run_id,
        "scene_analyses": [{
            "scene_id": 1,
            "start_sec": 0.0,
            "end_sec": 4.0,
            "summary": "오프라인 계약 검증 씬 요약",
            "visual_ui_text": "[화면] 버튼",
            "spoken_summary": "[자막] 오프라인 계약 테스트 자막 1",
            "confidence": 0.95,
            "verification_status": "SELF_CHECKED",
            "evidence_ref": "board_01.png (PANEL #01), s000001",
        }],
        "prompts_extracted": [],
        "ui_settings_extracted": [],
    }))

    verification_path = run_dir / "verification.json"
    atomic_write(verification_path, json_text({
        "video_id": video_id,
        "run_id": run_id,
        "total_items": 1,
        "status_counts": {"SELF_CHECKED": 1},
        "items": [{"scene_id": 1, "final_status": "SELF_CHECKED", "timestamp": "00:01"}],
    }))

    report_path = run_dir / "final_report.md"
    atomic_write(report_path, "# Offline Worker Contract Report Fixture\n\nVerified offline.\n")

    stage_plan: List[Tuple[str, List[Path], List[Path]]] = [
        ("media_cached", [video_file, sub_file], [media_cache_file]),
        ("subtitles_parsed", [media_cache_file, sub_file], [parsed_sub_path]),
        ("metadata_probed", [parsed_sub_path, media_cache_file, video_file], [meta_path]),
        ("scenes_detected", [meta_path, video_file], [scenes_path]),
        ("frames_extracted", [scenes_path, parsed_sub_path], [frames_manifest_path, frame_png, board_png]),
        ("timeline_aligned", [frames_manifest_path, scenes_path, parsed_sub_path], [aligned_path]),
        ("tiered_analyzed", [aligned_path, frames_manifest_path], [analysis_path]),
        ("verification_saved", [analysis_path], [verification_path]),
        ("markdown_assembled", [verification_path, analysis_path, parsed_sub_path, meta_path], [report_path]),
    ]

    ckpt_stages: Dict[str, Any] = {}
    recorded_outputs: List[Path] = []
    for idx, (st_name, in_files, out_files) in enumerate(stage_plan[:up_to_stage]):
        in_manifest = build_stage_artifact_manifest(root_resolved, video_id, in_files)
        out_manifest = build_stage_artifact_manifest(root_resolved, video_id, out_files)
        record_verified_stage_artifact(
            root_resolved,
            contract,
            stage_name=st_name,
            input_manifest=in_manifest,
            output_manifest=out_manifest,
            now_utc=now_utc + timedelta(seconds=idx),
        )
        ckpt_stages[st_name] = {
            "status": "DONE",
            "completed_utc": (now_utc + timedelta(seconds=idx)).isoformat(),
            "artifact_path": out_files[0].relative_to(root_resolved).as_posix(),
            "metadata": {},
        }
        recorded_outputs.append(out_files[0])

    ckpt_doc = {
        "schema_version": 1,
        "video_id": video_id,
        "run_id": run_id,
        "status": "COMPLETED" if up_to_stage == len(MULTIMODAL_STAGES) else "IN_PROGRESS",
        "last_completed_stage": MULTIMODAL_STAGES[up_to_stage - 1] if up_to_stage > 0 else None,
        "stages": ckpt_stages,
        "updated_utc": (now_utc + timedelta(seconds=up_to_stage)).isoformat(),
    }
    atomic_write(run_dir / "multimodal_checkpoint.json", json_text(ckpt_doc))
    return recorded_outputs


def build_synthetic_stream_json_usage(
    *,
    model: str = DEFAULT_FLASH_MODEL,
    turns: Sequence[Tuple[int, int, int]] = ((1200, 300, 180), (800, 200, 120)),
    cumulative_override: Optional[Tuple[int, int, int]] = None,
) -> str:
    """Create a synthetic AGY stream-json payload with init, step_update turns, and result."""
    lines: List[str] = [
        json.dumps({"event": "init", "init": {"model": model}}),
    ]
    for idx, (u_in, c_read, u_out) in enumerate(turns, start=1):
        lines.append(json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": idx,
                "step_type": "planner",
                "state": "DONE",
                "usage": {
                    "input_tokens": u_in,
                    "cache_read_tokens": c_read,
                    "output_tokens": u_out,
                },
            },
        }))
    if cumulative_override is not None:
        c_in, c_read, c_out = cumulative_override
    else:
        c_in = sum(t[0] for t in turns)
        c_read = sum(t[1] for t in turns)
        c_out = sum(t[2] for t in turns)
    lines.append(json.dumps({
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "duration_seconds": 2.5,
            "usage": {
                "input_tokens": c_in,
                "cache_read_tokens": c_read,
                "output_tokens": c_out,
            },
        },
    }))
    return "\n".join(lines) + "\n"


def run_phase5e_contract_validation_suite() -> Dict[str, Any]:
    """Execute scenarios E01-E16 in isolated temporary folders with external calls blocked."""
    scenarios: List[Dict[str, Any]] = []
    with offline_external_call_guard() as tracker:
        def _record(sid: str, name: str, kind: str, category: Optional[str], code: str) -> None:
            scenarios.append({
                "scenario_id": sid,
                "name": name,
                "scenario_type": kind,
                "error_category": category,
                "result_code": code,
                "status": "PASS",
            })

        # E01: Normal end-to-end offline contract validation & completion
        with tempfile.TemporaryDirectory(prefix="p5e_e01_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e01",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            validate_worker_input_contract(root, c, now_utc=t0)
            materialize_and_record_nine_stages(root, c, now_utc=t0)
            res = finish_worker_contract_job(
                root, c, success=True,
                usage_source=build_synthetic_stream_json_usage(),
                now_utc=t0 + timedelta(seconds=15),
            )
            assert res["status"] == "SIMULATED"
            _record("E01", "valid_offline_worker_contract_end_to_end", "NORMAL", None, "SIMULATED")

        # E02: Stale plan rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e02_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e02",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            (root / "data" / "videos" / f"{2:011d}" / "media_cache.json").unlink()
            try:
                validate_worker_input_contract(root, c, now_utc=t0)
                raise AssertionError("E02 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "INPUT_UNAVAILABLE_OR_BLOCKED"
                _record("E02", "stale_plan_rejected", "ERROR", exc.category, exc.code)

        # E03: Invalid lease rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e03_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e03",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token="0" * 48,
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            try:
                validate_worker_input_contract(root, c, now_utc=t0)
                raise AssertionError("E03 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "AUTHORIZATION_FAILURE"
                _record("E03", "invalid_lease_rejected", "ERROR", exc.category, exc.code)

        # E04: Expired lease rejected & manual recovery
        with tempfile.TemporaryDirectory(prefix="p5e_e04_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e04",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            late = t0 + timedelta(minutes=31)
            try:
                validate_worker_input_contract(root, c, now_utc=late)
                raise AssertionError("E04 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "LEASE_EXPIRED"
                recover_batch_job(
                    root, sha, video_id=claim["video_id"], actor="op",
                    reason="Manual recovery after synthetic lease expiry check.", now_utc=late,
                )
                reconcile_paused_reservation(
                    root, sha, claim["video_id"], actor="op",
                    reason="Reconciled zero token spend on expired lease.",
                    usage={"uncached_input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0},
                    now_utc=late,
                )
                retried = recover_batch_job(
                    root, sha, video_id=claim["video_id"], actor="op",
                    reason="Manual retry after expired lease reconciliation.",
                    retry=True, now_utc=late,
                )
                assert retried["status"] == "PENDING"
                _record("E04", "expired_lease_and_manual_recovery", "RECOVERY", exc.category, exc.code)

        # E05: Stage reorder rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e05_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e05",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            vdir = root / "data" / "videos" / claim["video_id"]
            m = build_stage_artifact_manifest(root, claim["video_id"], [vdir / "media_cache.json"])
            try:
                record_verified_stage_artifact(
                    root, c, stage_name="subtitles_parsed", input_manifest=m, output_manifest=m, now_utc=t0,
                )
                raise AssertionError("E05 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "ARTIFACT_INTEGRITY_FAILURE"
                _record("E05", "stage_reorder_rejected", "ERROR", exc.category, exc.code)

        # E06: Digest mismatch rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e06_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e06",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            vdir = root / "data" / "videos" / claim["video_id"]
            in_m = build_stage_artifact_manifest(
                root, claim["video_id"], [vdir / "media" / "video.mp4", vdir / "media" / "captions.json3"],
            )
            out_m = build_stage_artifact_manifest(root, claim["video_id"], [vdir / "media_cache.json"])
            out_m["artifacts"][0]["sha256"] = "f" * 64
            try:
                record_verified_stage_artifact(
                    root, c, stage_name="media_cached", input_manifest=in_m, output_manifest=out_m, now_utc=t0,
                )
                raise AssertionError("E06 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "ARTIFACT_INTEGRITY_FAILURE"
                _record("E06", "digest_mismatch_rejected", "ERROR", exc.category, exc.code)

        # E07: Modified artifact rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e07_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e07",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            materialize_and_record_nine_stages(root, c, now_utc=t0)
            p_sub = root / "data" / "videos" / claim["video_id"] / "parsed_subtitles.json"
            raw_b = bytearray(p_sub.read_bytes())
            raw_b[-3] = ord("Z")
            p_sub.write_bytes(bytes(raw_b))
            try:
                verified_stage_status(root, sha, claim["video_id"])
                raise AssertionError("E07 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "ARTIFACT_INTEGRITY_FAILURE"
                _record("E07", "modified_artifact_rejected", "ERROR", exc.category, exc.code)

        # E08: Path traversal rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e08_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e08",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            bad_m = {
                "schema_version": 1,
                "video_id": claim["video_id"],
                "artifacts": [{"name": "x", "path": "../outside.json", "size_bytes": 2, "sha256": "a" * 64}],
            }
            try:
                record_verified_stage_artifact(
                    root, c, stage_name="media_cached", input_manifest=bad_m, output_manifest=bad_m, now_utc=t0,
                )
                raise AssertionError("E08 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "ARTIFACT_INTEGRITY_FAILURE"
                _record("E08", "path_traversal_rejected", "ERROR", exc.category, exc.code)

        # E09: Missing usage rejected & reservation retained
        with tempfile.TemporaryDirectory(prefix="p5e_e09_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e09",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            materialize_and_record_nine_stages(root, c, now_utc=t0)
            try:
                finish_worker_contract_job(root, c, success=True, usage_source=None, now_utc=t0 + timedelta(seconds=10))
                raise AssertionError("E09 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "USAGE_UNKNOWN"
                assert batch_budget_status(root, sha, now_utc=t0)["global"]["unsettled_reservations"] == 1
                _record("E09", "missing_usage_retains_reservation", "ERROR", exc.category, exc.code)

        # E10: Duplicate cumulative usage rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e10_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            try:
                normalize_agy_usage_contract(
                    build_synthetic_stream_json_usage(
                        turns=((1000, 100, 100), (500, 50, 50)),
                        cumulative_override=(3000, 300, 300),
                    ),
                    expected_model_id=DEFAULT_FLASH_MODEL,
                    plan_sha256=sha,
                    video_id=f"{1:011d}",
                    run_id="r_e10",
                    attempt=1,
                )
                raise AssertionError("E10 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "USAGE_UNKNOWN"
                _record("E10", "duplicate_cumulative_usage_rejected", "ERROR", exc.category, exc.code)

        # E11: Over-budget usage rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e11_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e11",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            materialize_and_record_nine_stages(root, c, now_utc=t0)
            try:
                finish_worker_contract_job(
                    root, c, success=True,
                    usage_source=build_synthetic_stream_json_usage(turns=((9000, 0, 100), (5000, 0, 100))),
                    now_utc=t0 + timedelta(seconds=10),
                )
                raise AssertionError("E11 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "CONTRACT_VIOLATION"
                _record("E11", "over_budget_usage_rejected", "ERROR", exc.category, exc.code)

        # E12: 429 global pause
        with tempfile.TemporaryDirectory(prefix="p5e_e12_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e12",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            out = finish_worker_contract_job(
                root, c, success=False, provider_error_code="429",
                retry_after_seconds=60, usage_source=None, now_utc=t0 + timedelta(seconds=1),
            )
            assert out["global_rate_paused"] and out["error_category"] == "GLOBAL_RATE_LIMIT"
            _record("E12", "429_global_pause_enforced", "ERROR", "GLOBAL_RATE_LIMIT", "429")

        # E13: Early resume rejected before cooldown and reconciliation
        with tempfile.TemporaryDirectory(prefix="p5e_e13_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e13",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            finish_worker_contract_job(
                root, c, success=False, provider_error_code="429",
                retry_after_seconds=60, usage_source=None, now_utc=t0 + timedelta(seconds=1),
            )
            try:
                resume_global_rate_limit(
                    root, sha, actor="op",
                    reason="Premature resume attempt before cooldown expires.",
                    now_utc=t0 + timedelta(seconds=20),
                )
                raise AssertionError("E13 expected failure")
            except BatchBudgetError as exc:
                info = classify_worker_error(exc)
                reconcile_paused_reservation(
                    root, sha, claim["video_id"], actor="op",
                    reason="Reconciled unknown 429 usage after local inspection.",
                    usage={"uncached_input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0},
                    now_utc=t0 + timedelta(seconds=70),
                )
                resumed = resume_global_rate_limit(
                    root, sha, actor="op",
                    reason="Manual resume after cooldown and reconciliation.",
                    now_utc=t0 + timedelta(seconds=71),
                )
                assert not resumed["rate_limit"]["global_paused"]
                _record("E13", "early_resume_rejected_then_manual_recovery", "RECOVERY", info["error_category"], info["error_code"])

        # E14: After-commit replay idempotency
        with tempfile.TemporaryDirectory(prefix="p5e_e14_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e14",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            vdir = root / "data" / "videos" / claim["video_id"]
            in_m = build_stage_artifact_manifest(
                root, claim["video_id"], [vdir / "media" / "video.mp4", vdir / "media" / "captions.json3"],
            )
            out_m = build_stage_artifact_manifest(root, claim["video_id"], [vdir / "media_cache.json"])
            try:
                record_verified_stage_artifact(
                    root, c, stage_name="media_cached", input_manifest=in_m, output_manifest=out_m,
                    now_utc=t0, inject_fault="after_commit",
                )
                raise AssertionError("E14 expected crash")
            except InjectedSyntheticCrash:
                rep = record_verified_stage_artifact(
                    root, c, stage_name="media_cached", input_manifest=in_m, output_manifest=out_m,
                    now_utc=t0 + timedelta(seconds=1),
                )
                assert rep["replayed_existing_checkpoint"] and rep["stages_done"] == 1
                _record("E14", "after_commit_replay_idempotency", "RECOVERY", None, "IDEMPOTENT_REPLAY")

        # E15: Unauthorized external call blocked
        try:
            socket.create_connection(("example.com", 443), timeout=1)
            raise AssertionError("E15 expected failure")
        except WorkerContractError as exc:
            assert exc.category == "AUTHORIZATION_FAILURE"
            _record("E15", "unauthorized_external_call_blocked", "ERROR", exc.category, exc.code)

        # E16: Partial completion falsely marked complete rejected
        with tempfile.TemporaryDirectory(prefix="p5e_e16_") as tmp:
            ws = create_phase5e_isolated_workspace(Path(tmp))
            root, sha, t0 = ws["root"], ws["plan_sha256"], ws["t0"]
            claim = claim_batch_job(root, sha, owner="w", model=DEFAULT_FLASH_MODEL, now_utc=t0)
            c = build_worker_input_contract(
                root, plan_sha256=sha, video_id=claim["video_id"], run_id="r_e16",
                exact_model_id=DEFAULT_FLASH_MODEL, lease_token=claim["lease_token"],
                lease_expires_utc=claim["lease_expires_utc"], attempt=1,
            )
            materialize_and_record_nine_stages(root, c, now_utc=t0, up_to_stage=2)
            ckpt_file = root / "data" / "videos" / claim["video_id"] / "runs" / "r_e16" / "multimodal_checkpoint.json"
            doc = json.loads(ckpt_file.read_text(encoding="utf-8"))
            doc["status"] = "COMPLETED"
            ckpt_file.write_text(json.dumps(doc), encoding="utf-8")
            try:
                finish_worker_contract_job(
                    root, c, success=True,
                    usage_source=build_synthetic_stream_json_usage(),
                    now_utc=t0 + timedelta(seconds=5),
                )
                raise AssertionError("E16 expected failure")
            except WorkerContractError as exc:
                assert exc.category == "ARTIFACT_INTEGRITY_FAILURE"
                _record("E16", "partial_completion_falsely_marked_complete_rejected", "ERROR", exc.category, exc.code)

    by_type = {
        kind: sum(1 for s in scenarios if s["scenario_type"] == kind)
        for kind in ("NORMAL", "ERROR", "RECOVERY")
    }
    return {
        "mode": "OFFLINE_CONTRACT_VALIDATION_ONLY",
        "total_scenarios": len(scenarios),
        "passed_scenarios": sum(1 for s in scenarios if s["status"] == "PASS"),
        "failed_scenarios": sum(1 for s in scenarios if s["status"] != "PASS"),
        "by_scenario_type": by_type,
        "external_calls": tracker.to_dict(),
        "scenarios": scenarios,
    }
