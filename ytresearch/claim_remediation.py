"""Run-local review of audited claim corrections; no model/network or implicit approval.

OPEN -> CORRECTED -> RECHECKED -> RESOLVED concerns a separate correction
artifact. The original report is NEVER made approvable by closing an issue:
the original 19-section text still contains the audited problematic claim.
Each transition is explicit, bound to the original immutable audit and a
versioned correction document. SHA chains catch accidental modification, not
malicious modification by someone with filesystem access.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

from .claim_adjudication import (
    SIDECAR_NAME, ClaimAuditInvalid, _run_dir,
    summarize_audited_claims,
)
from .storage import atomic_write, json_text, read_json
from .evidence_refs import reconcile_analysis_evidence


LEDGER_NAME = "claim_remediation_events.json"
ISSUE_TYPES = ("CONFIRMED_ERROR", "SOURCE_CONFLICT")
NEXT = {"OPEN": "CORRECTED", "CORRECTED": "RECHECKED", "RECHECKED": "RESOLVED"}
DECISION = "HUMAN_RECHECK_RECORDED_NOT_AUTOMATIC_TRUTH"


class ClaimRemediationInvalid(ValueError):
    pass


def _digest(data: Any) -> str:
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _under_corrections(run_dir: Path, name: str) -> Path:
    candidate = (run_dir / "corrections" / name).resolve()
    parent = (run_dir / "corrections").resolve()
    if not candidate.is_relative_to(parent) or candidate == parent or candidate.suffix != ".json":
        raise ClaimRemediationInvalid("CORRECTION_PATH_INVALID")
    if not candidate.is_file():
        raise ClaimRemediationInvalid("CORRECTION_ARTIFACT_MISSING")
    return candidate


def _source(root: Path, video_id: str, run_id: str):
    run_dir = _run_dir(root, video_id, run_id)
    audit_path = run_dir / SIDECAR_NAME
    if not audit_path.is_file():
        raise ClaimRemediationInvalid("IMPORTED_CLAIM_AUDIT_REQUIRED")
    # Revalidates audit against the original source analysis/frames/subtitles.
    audited = summarize_audited_claims(root, video_id, run_id)
    audit = read_json(audit_path)
    allowed = {
        row["item_index"]: row for row in audited["items"]
        if row["finding_status"] in ISSUE_TYPES
    }
    return run_dir, audit_path, audit, allowed


def _check_correction(run_dir: Path, filename: str, issue: Dict[str, Any],
                      *, expected_sha: Optional[str] = None) -> str:
    p = _under_corrections(run_dir, filename)
    raw_hash = _file_sha(p)
    if expected_sha and raw_hash != expected_sha:
        raise ClaimRemediationInvalid("CORRECTION_ARTIFACT_HASH_CHANGED")
    doc = read_json(p)
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise ClaimRemediationInvalid("CORRECTION_SCHEMA_INVALID")
    if doc.get("item_index") != issue["item_index"]:
        raise ClaimRemediationInvalid("CORRECTION_ITEM_MISMATCH")
    analysis = read_json(run_dir / "tiered_analysis.json")
    original_claim = analysis["research_items"][issue["item_index"] - 1]
    if doc.get("original_claim_sha256") != hashlib.sha256(
        str(original_claim["text"]).encode("utf-8")
    ).hexdigest():
        raise ClaimRemediationInvalid("CORRECTION_ORIGINAL_CLAIM_HASH_MISMATCH")
    correction = str(doc.get("corrected_text") or "").strip()
    if len(correction) < 20:
        raise ClaimRemediationInvalid("CORRECTION_TEXT_TOO_SHORT")
    if len(str(doc.get("change_reason") or "").strip()) < 30:
        raise ClaimRemediationInvalid("CORRECTION_REASON_MISSING")
    refs = doc.get("evidence_refs", []) or []
    if not isinstance(refs, list) or not isinstance(doc.get("external_sources", []), list):
        raise ClaimRemediationInvalid("CORRECTION_EVIDENCE_LIST_INVALID")
    if not refs and not doc.get("external_sources"):
        raise ClaimRemediationInvalid("CORRECTION_EVIDENCE_MISSING")
    if refs:
        if any(not isinstance(ref, str) for ref in refs):
            raise ClaimRemediationInvalid("CORRECTION_BOARD_REF_INVALID")
        try:
            reconcile_analysis_evidence(
                {"research_items": [{"evidence_ref": ref} for ref in refs]},
                read_json(run_dir / "frames_manifest.json"),
            )
        except (ValueError, TypeError) as exc:
            raise ClaimRemediationInvalid("CORRECTION_BOARD_REF_INVALID") from exc
    for url in doc.get("external_sources", []):
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ClaimRemediationInvalid("CORRECTION_EXTERNAL_URL_INVALID")
    return raw_hash


def _new_ledger(video_id: str, run_id: str, audit_sha: str) -> Dict[str, Any]:
    return {
        "schema_version": 1, "video_id": video_id, "run_id": run_id,
        "source_audit_sha256": audit_sha,
        "events": [],
        "limitation": ("Human correction review is attested, not independently verified "
                       "by this code. The original audited report remains blocked."),
    }


def _replay(run_dir: Path, data: Dict[str, Any],
            allowed: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    stages = {i: {"status": "OPEN", "correction_file": None, "correction_sha256": None,
                  "author": None, "rechecker": None}
              for i in allowed}
    last = "GENESIS"
    for sequence, event in enumerate(data.get("events", []), 1):
        if not isinstance(event, dict) or event.get("sequence") != sequence:
            raise ClaimRemediationInvalid("REMEDIATION_EVENT_SEQUENCE_INVALID")
        digest = event.get("event_sha256")
        body = {k: v for k, v in event.items() if k != "event_sha256"}
        if event.get("previous_sha256") != last or digest != _digest(body):
            raise ClaimRemediationInvalid("REMEDIATION_CHAIN_INVALID")
        index = event.get("item_index")
        if type(index) is not int or index not in allowed:
            raise ClaimRemediationInvalid("REMEDIATION_UNKNOWN_ISSUE")
        stage = stages[index]
        if NEXT.get(stage["status"]) != event.get("next_status"):
            raise ClaimRemediationInvalid("REMEDIATION_INVALID_TRANSITION")
        if not str(event.get("actor") or "").strip():
            raise ClaimRemediationInvalid("REMEDIATION_ACTOR_MISSING")
        if len(str(event.get("reason") or "").strip()) < 30:
            raise ClaimRemediationInvalid("REMEDIATION_REASON_MISSING")
        try:
            dt = datetime.fromisoformat(event["timestamp_utc"])
            if dt.tzinfo is None:
                raise ValueError("timezone required")
        except (ValueError, TypeError, KeyError) as exc:
            raise ClaimRemediationInvalid("REMEDIATION_TIMESTAMP_INVALID") from exc
        if event["next_status"] == "CORRECTED":
            name = event.get("correction_file")
            if not isinstance(name, str):
                raise ClaimRemediationInvalid("CORRECTION_FILE_REQUIRED")
            _check_correction(run_dir, name, allowed[index],
                              expected_sha=event.get("correction_sha256"))
            stage.update(correction_file=name, correction_sha256=event["correction_sha256"],
                         author=event["actor"])
        else:
            if (event.get("correction_file") != stage["correction_file"] or
                    event.get("correction_sha256") != stage["correction_sha256"]):
                raise ClaimRemediationInvalid("REMEDIATION_CORRECTION_VERSION_CHANGED")
            _check_correction(run_dir, stage["correction_file"], allowed[index],
                              expected_sha=stage["correction_sha256"])
            if event["next_status"] == "RECHECKED":
                if event["actor"] == stage["author"]:
                    raise ClaimRemediationInvalid("RECHECK_REQUIRES_DIFFERENT_REVIEWER")
                if event.get("recheck_disposition") != DECISION:
                    raise ClaimRemediationInvalid("RECHECK_DECISION_REQUIRED")
                stage["rechecker"] = event["actor"]
            if event["next_status"] == "RESOLVED":
                if not stage["rechecker"] or event["actor"] == stage["author"]:
                    raise ClaimRemediationInvalid("RESOLUTION_REQUIRES_REVIEWER")
        stage["status"] = event["next_status"]
        last = digest
    return stages


def summarize_claim_remediations(root: Path, video_id: str, run_id: str) -> Dict[str, Any]:
    run_dir, audit_file, audit, allowed = _source(root, video_id, run_id)
    ledger_path = run_dir / LEDGER_NAME
    audit_hash = _file_sha(audit_file)
    if ledger_path.is_file():
        ledger = read_json(ledger_path)
        if not isinstance(ledger, dict) or ledger.get("schema_version") != 1:
            raise ClaimRemediationInvalid("REMEDIATION_LEDGER_INVALID")
        if ledger.get("video_id") != video_id or ledger.get("run_id") != run_id:
            raise ClaimRemediationInvalid("REMEDIATION_ID_MISMATCH")
        if ledger.get("source_audit_sha256") != audit_hash:
            raise ClaimRemediationInvalid("REMEDIATION_SOURCE_AUDIT_CHANGED")
    else:
        ledger = _new_ledger(video_id, run_id, audit_hash)
    stages = _replay(run_dir, ledger, allowed)
    counts = {x: sum(v["status"] == x for v in stages.values())
              for x in ("OPEN", "CORRECTED", "RECHECKED", "RESOLVED")}
    return {
        "schema_version": 1,
        "video_id": video_id, "run_id": run_id,
        "audit_id": audit["audit_id"],
        "source_audit_sha256": audit_hash,
        "issue_count": len(stages),
        "status_counts": counts,
        "issues": [
            {"item_index": i, "finding_status": allowed[i]["finding_status"],
             "resolution_status": info["status"],
             "correction_file": info["correction_file"],
             "correction_sha256": info["correction_sha256"],
             "author": info["author"], "rechecker": info["rechecker"]}
            for i, info in sorted(stages.items())
        ],
        "event_count": len(ledger["events"]),
        "original_report_approval_blocked": bool(allowed),
        "recheck_is_attestation_not_truth": True,
    }


def record_claim_remediation(
    root: Path, video_id: str, run_id: str, *,
    item_index: int, next_status: str, actor: str, reason: str,
    correction_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Explicit local user action. No automatic state transitions or AGY call."""
    run_dir, audit_file, _, allowed = _source(root, video_id, run_id)
    if item_index not in allowed:
        raise ClaimRemediationInvalid("REMEDIATION_NOT_A_CONFIRMED_ISSUE")
    if not actor or len(reason.strip()) < 30:
        raise ClaimRemediationInvalid("REMEDIATION_ACTOR_AND_REASON_REQUIRED")
    path = run_dir / LEDGER_NAME
    ledger = read_json(path) if path.is_file() else _new_ledger(
        video_id, run_id, _file_sha(audit_file),
    )
    # Validate existing events and artifact hashes before adding to chain.
    summary = summarize_claim_remediations(root, video_id, run_id)
    stage = next((r for r in summary["issues"] if r["item_index"] == item_index), None)
    if stage is None or NEXT.get(stage["resolution_status"]) != next_status:
        raise ClaimRemediationInvalid("REMEDIATION_INVALID_TRANSITION")
    if next_status == "CORRECTED":
        if not correction_file:
            raise ClaimRemediationInvalid("CORRECTION_FILE_REQUIRED")
        artifact = _under_corrections(run_dir, correction_file)
        # Require the corrected document to differ from the actual original
        # claim, not just be a renamed copy of it.
        original = read_json(run_dir / "tiered_analysis.json")["research_items"][item_index - 1]
        doc = read_json(artifact)
        if str(doc.get("corrected_text", "")).strip() == str(original["text"]).strip():
            raise ClaimRemediationInvalid("CORRECTION_UNCHANGED_ORIGINAL_CLAIM")
        sha = _check_correction(run_dir, correction_file, stage)
    else:
        correction_file = stage["correction_file"]
        sha = stage["correction_sha256"]
        if next_status == "RECHECKED" and actor == stage["author"]:
            raise ClaimRemediationInvalid("RECHECK_REQUIRES_DIFFERENT_REVIEWER")
        if next_status == "RESOLVED" and actor == stage["author"]:
            raise ClaimRemediationInvalid("RESOLUTION_REQUIRES_REVIEWER")
    previous = ledger["events"][-1]["event_sha256"] if ledger["events"] else "GENESIS"
    event = {
        "sequence": len(ledger["events"]) + 1,
        "item_index": item_index, "next_status": next_status,
        "actor": actor, "reason": reason.strip(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "correction_file": correction_file, "correction_sha256": sha,
        "recheck_disposition": DECISION if next_status == "RECHECKED" else None,
        "previous_sha256": previous,
    }
    event["event_sha256"] = _digest(event)
    ledger["events"].append(event)
    # Re-validate chain including newly appended transition before commit.
    _replay(run_dir, ledger, allowed)
    atomic_write(path, json_text(ledger))
    return summarize_claim_remediations(root, video_id, run_id)
