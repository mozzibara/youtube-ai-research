"""Read-only, version-bound human audit findings for individual research claims.

Audit/import is OFFLINE. Imported findings are not model truth, do not change
risk priority, never auto-approve a report, and do not alter original media,
analysis JSON, frame manifests or published reports.

Separate dimensions:
 * review_priority: P0/P1/P2 deterministic triage (claim_review.py)
 * finding_status: CONFIRMED_ERROR / SOURCE_CONFLICT / UNVERIFIED / NOT_REVIEWED
 * source_match: MATCH / PARTIAL / CONFLICT / NOT_REVIEWED
 * external_verification: USER_AUDIT_REPORTED, NOT independently rechecked here
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .evidence_refs import reconcile_analysis_evidence
from .research_state import RUN_ID, VIDEO_ID
from .storage import atomic_write, json_text, read_json


AUDIT_STATUSES = ("CONFIRMED_ERROR", "SOURCE_CONFLICT", "UNVERIFIED", "NOT_REVIEWED")
AUDITED_STATUSES = set(AUDIT_STATUSES[:-1])
SOURCE_MATCHES = ("MATCH", "PARTIAL", "CONFLICT", "NOT_REVIEWED")
SIDECAR_NAME = "claim_adjudications.json"
IMPORT_ORIGIN = "USER_SUPPLIED_MANUAL_SOURCE_AUDIT"


class ClaimAuditInvalid(ValueError):
    """Unverifiable identity, mutated source, invalid citation or stale claim."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _run_dir(root: Path, video_id: str, run_id: str) -> Path:
    if not VIDEO_ID.fullmatch(video_id) or not RUN_ID.fullmatch(run_id):
        raise ClaimAuditInvalid("AUDIT_INVALID_VIDEO_OR_RUN_ID")
    return Path(root) / "data" / "videos" / video_id / "runs" / run_id


def _source_docs(root: Path, video_id: str, run_id: str):
    run_dir = _run_dir(root, video_id, run_id)
    analysis_path = run_dir / "tiered_analysis.json"
    manifest_path = run_dir / "frames_manifest.json"
    subtitles_path = run_dir.parent.parent / "parsed_subtitles.json"
    # All must exist, otherwise refuse to attach an unverifiable audit.
    analysis_bytes = analysis_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    subtitles_bytes = subtitles_path.read_bytes()
    return (
        read_json(analysis_path), read_json(manifest_path),
        read_json(subtitles_path),
        {
            "source_analysis_sha256": _sha(analysis_bytes),
            "source_frames_manifest_sha256": _sha(manifest_bytes),
            "source_subtitles_sha256": _sha(subtitles_bytes),
        },
    )


def validate_audit_document(
    audit: Mapping[str, Any], analysis: Mapping[str, Any],
    manifest: Mapping[str, Any], subtitles: Mapping[str, Any],
    actual_hashes: Mapping[str, str],
) -> Dict[str, Any]:
    if audit.get("schema_version") != 1 or not str(audit.get("audit_id") or ""):
        raise ClaimAuditInvalid("AUDIT_SCHEMA_OR_ID_INVALID")
    if audit.get("audit_origin") != IMPORT_ORIGIN:
        raise ClaimAuditInvalid("AUDIT_PROVENANCE_UNSUPPORTED")
    for name, digest in actual_hashes.items():
        if audit.get(name) != digest:
            raise ClaimAuditInvalid(f"AUDIT_SOURCE_HASH_MISMATCH:{name}")

    findings = audit.get("findings")
    if not isinstance(findings, list) or not findings:
        raise ClaimAuditInvalid("AUDIT_FINDINGS_MISSING")
    claims = analysis.get("research_items", []) or []
    known_subtitles = {
        str(s.get("id") or s.get("segment_id"))
        for s in (subtitles.get("segments", []) or [])
    }
    reviewed = set()
    counts = {status: 0 for status in AUDIT_STATUSES}
    for entry in findings:
        if not isinstance(entry, dict):
            raise ClaimAuditInvalid("AUDIT_ENTRY_NOT_OBJECT")
        index = entry.get("item_index")
        if (type(index) is not int or not 1 <= index <= len(claims)
            or index in reviewed):
            raise ClaimAuditInvalid("AUDIT_ITEM_INDEX_DUPLICATED_OR_INVALID")
        reviewed.add(index)
        original = claims[index - 1]
        if original.get("section_id") != entry.get("section_id"):
            raise ClaimAuditInvalid(f"AUDIT_SECTION_MISMATCH:{index}")
        if _sha(str(original.get("text", "")).encode("utf-8")) != entry.get("claim_sha256"):
            raise ClaimAuditInvalid(f"AUDIT_CLAIM_HASH_MISMATCH:{index}")
        status = entry.get("finding_status")
        match = entry.get("source_match")
        if status not in AUDITED_STATUSES or match not in SOURCE_MATCHES:
            raise ClaimAuditInvalid(f"AUDIT_STATUS_INVALID:{index}")
        if status == "SOURCE_CONFLICT" and match != "CONFLICT":
            raise ClaimAuditInvalid(f"AUDIT_CONFLICT_WITHOUT_SOURCE_CONFLICT:{index}")
        if status == "CONFIRMED_ERROR" and not entry.get("external_sources"):
            raise ClaimAuditInvalid(f"AUDIT_CONFIRMED_ERROR_WITHOUT_EXTERNAL_SOURCE:{index}")
        if len(str(entry.get("review_note") or "").strip()) < 30:
            raise ClaimAuditInvalid(f"AUDIT_RATIONALE_MISSING:{index}")
        if len(str(entry.get("correction") or "").strip()) < 20:
            raise ClaimAuditInvalid(f"AUDIT_CORRECTION_MISSING:{index}")
        evidence_refs = entry.get("evidence_refs") or []
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise ClaimAuditInvalid(f"AUDIT_VISUAL_EVIDENCE_MISSING:{index}")
        original_ref = str(original.get("evidence_ref") or "")
        if any(not isinstance(ref, str) or ref not in original_ref for ref in evidence_refs):
            raise ClaimAuditInvalid(f"AUDIT_REF_NOT_IN_ORIGINAL_CLAIM:{index}")
        try:
            reconcile_analysis_evidence(
                {"research_items": [{"evidence_ref": ref} for ref in evidence_refs]},
                manifest,
            )
        except (ValueError, TypeError) as exc:
            raise ClaimAuditInvalid(f"AUDIT_BAD_BOARD_PANEL:{index}") from exc
        ids = entry.get("subtitle_ids") or []
        if not isinstance(ids, list) or any(str(i) not in known_subtitles for i in ids):
            raise ClaimAuditInvalid(f"AUDIT_SUBTITLE_ID_MISSING:{index}")
        sources = entry.get("external_sources", [])
        if not isinstance(sources, list) or any(
            not isinstance(s, str) or not s.startswith("https://") for s in sources
        ):
            raise ClaimAuditInvalid(f"AUDIT_EXTERNAL_SOURCE_INVALID:{index}")
        if not str(entry.get("trigger") or ""):
            raise ClaimAuditInvalid(f"AUDIT_TRIGGER_MISSING:{index}")
        counts[status] += 1
    counts["NOT_REVIEWED"] = len(claims) - len(reviewed)
    return {
        "source_claims": len(claims),
        "findings_count": len(findings),
        "status_counts": counts,
        "unreviewed_indices": [
            i for i in range(1, len(claims) + 1) if i not in reviewed
        ],
    }


def import_claim_audit(root: Path, audit_file: Path) -> Dict[str, Any]:
    """Explicit offline import to a run-scoped sidecar; no model calls."""
    audit = read_json(Path(audit_file))
    if not isinstance(audit, dict):
        raise ClaimAuditInvalid("AUDIT_NOT_OBJECT")
    video_id, run_id = str(audit.get("video_id", "")), str(audit.get("run_id", ""))
    run_dir = _run_dir(root, video_id, run_id)
    analysis, frames, subtitles, hashes = _source_docs(root, video_id, run_id)
    summary = validate_audit_document(audit, analysis, frames, subtitles, hashes)
    dst = run_dir / SIDECAR_NAME
    if dst.exists() and dst.read_bytes() != json_text(audit).encode("utf-8"):
        raise ClaimAuditInvalid("AUDIT_SIDECAR_ALREADY_PRESENT_NO_OVERWRITE")
    if not dst.exists():
        atomic_write(dst, json_text(audit))
    return {"video_id": video_id, "run_id": run_id, "audit_id": audit["audit_id"],
            **summary, "sidecar_path": str(dst),
            "model_called": False, "automatic_fact_check_performed": False}


def summarize_audited_claims(
    root: Path, video_id: str, run_id: str,
    *, analysis: Optional[Mapping[str, Any]] = None,
    frames: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Get validated sidecar and all per-claim statuses; fail closed if stale."""
    run_dir = _run_dir(root, video_id, run_id)
    path = run_dir / SIDECAR_NAME
    if analysis is None or frames is None or path.is_file():
        # Source bytes and checksum matching must be checked on every read if
        # an audit is present. This prevents stale findings on modified runs.
        real_analysis, real_frames, subtitles, hashes = _source_docs(
            root, video_id, run_id
        )
        if analysis is None:
            analysis = real_analysis
        if frames is None:
            frames = real_frames
    claims = analysis.get("research_items", []) or []
    by_index = {}
    audit_id = None
    if path.is_file():
        audit = read_json(path)
        if audit.get("video_id") != video_id or audit.get("run_id") != run_id:
            raise ClaimAuditInvalid("AUDIT_VIDEO_OR_RUN_ID_MISMATCH")
        validate_audit_document(audit, real_analysis, real_frames, subtitles, hashes)
        audit_id = audit["audit_id"]
        by_index = {x["item_index"]: x for x in audit["findings"]}
    rows, counts = [], {status: 0 for status in AUDIT_STATUSES}
    for i, claim in enumerate(claims, 1):
        entry = by_index.get(i)
        status = entry["finding_status"] if entry else "NOT_REVIEWED"
        counts[status] += 1
        rows.append({
            "item_index": i,
            "section_id": claim.get("section_id"),
            "finding_status": status,
            "source_match": entry["source_match"] if entry else "NOT_REVIEWED",
            "trigger": entry["trigger"] if entry else None,
            "audit_id": audit_id if entry else None,
            "review_note": entry["review_note"] if entry else None,
            "correction": entry["correction"] if entry else None,
            "external_sources": list(entry["external_sources"]) if entry else [],
        })
    return {
        "schema_version": 1, "audit_id": audit_id,
        "reviewed_claims": len(by_index), "claims_total": len(claims),
        "status_counts": counts, "items": rows,
        "has_unresolved_confirmed_problem": bool(
            counts["CONFIRMED_ERROR"] or counts["SOURCE_CONFLICT"]
        ),
        "automatic_factual_verification": False,
        "audit_provenance": "USER_SUPPLIED_MANUAL_SOURCE_AUDIT" if audit_id else None,
        "note": (
            "These are audit-reported adjudications of selected source claims, "
            "NOT automatic factual validation or approval; other claims remain NOT_REVIEWED."
        ),
    }
