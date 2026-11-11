"""Phase 3: deterministic multimodal evidence quality gate (never factual approval).

Checks observable structural invariants against persisted files; it cannot infer
whole-video OCR accuracy, model truth, or human review from a passing test.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from .evidence_refs import InvalidEvidenceReference, reconcile_analysis_evidence
from .claim_review import summarize_claim_reviews
from .claim_adjudication import ClaimAuditInvalid, summarize_audited_claims
from .claim_remediation import ClaimRemediationInvalid, summarize_claim_remediations
from .research_state import RUN_ID, VIDEO_ID
from .storage import atomic_write, json_text, read_json


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _paths(root: Path, video_id: str, run_id: str) -> tuple[Path, Path]:
    if not VIDEO_ID.fullmatch(video_id) or not RUN_ID.fullmatch(run_id):
        raise ValueError("VIDEO_OR_RUN_ID_INVALID")
    base = Path(root) / "data" / "videos" / video_id
    return base, base / "runs" / run_id


def _load_required(run_dir: Path, filename: str, errors: List[str]) -> Dict[str, Any]:
    path = run_dir / filename
    try:
        doc = read_json(path)
        if not isinstance(doc, dict):
            raise ValueError("expected JSON object")
        return doc
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        errors.append("MISSING_OR_INVALID:" + filename)
        return {}


def assess_research_run(
    root: Path, video_id: str, run_id: str, *, persist: bool = True,
    verify_image_hashes: bool = True,
) -> Dict[str, Any]:
    """Conservative offline-only analysis. Always marks human review required."""
    vdir, run_dir = _paths(root, video_id, run_id)
    errors: List[str] = []
    warnings: List[str] = []
    observations: List[Dict[str, Any]] = []

    checkpoint = _load_required(run_dir, "multimodal_checkpoint.json", errors)
    manifest = _load_required(run_dir, "frames_manifest.json", errors)
    analysis = _load_required(run_dir, "tiered_analysis.json", errors)
    verification = _load_required(run_dir, "verification.json", errors)
    subtitles = _load_required(vdir, "parsed_subtitles.json", errors)
    if not (run_dir / "final_report.md").is_file():
        errors.append("MISSING_FINAL_EVIDENCE_REPORT")
    if checkpoint and (
        checkpoint.get("video_id") != video_id or checkpoint.get("run_id") != run_id
    ):
        errors.append("CHECKPOINT_VIDEO_RUN_MISMATCH")
    if checkpoint.get("status") != "COMPLETED":
        errors.append("CHECKPOINT_NOT_COMPLETED")

    frames = manifest.get("frames", []) or []
    boards = manifest.get("boards", []) or []
    rois = manifest.get("roi_frames", []) or []
    if not frames or not boards:
        errors.append("NO_FRAME_OR_BOARD_EVIDENCE")
    if manifest.get("frame_count", len(frames)) != len(frames):
        errors.append("FRAME_MANIFEST_COUNT_MISMATCH")
    if manifest.get("board_count", len(boards)) != len(boards):
        errors.append("BOARD_MANIFEST_COUNT_MISMATCH")
    if manifest.get("roi_frame_count", len(rois)) != len(rois):
        errors.append("ROI_MANIFEST_COUNT_MISMATCH")
    board_hash_checked = 0
    if verify_image_hashes:
        for board in boards:
            path = Path(str(board.get("path", "")))
            if not path.is_file():
                errors.append("BOARD_IMAGE_MISSING:" + path.name)
            elif board.get("sha256"):
                board_hash_checked += 1
                if _file_sha256(path) != str(board["sha256"]):
                    errors.append("BOARD_SHA256_MISMATCH:" + path.name)
            else:
                warnings.append("BOARD_WITHOUT_SHA256:" + path.name)

    areas = []
    for frame in frames:
        w = int(frame.get("width", 0))
        h = int(frame.get("height", 0))
        sw = int(frame.get("source_width", 0))
        sh = int(frame.get("source_height", 0))
        if min(w, h, sw, sh) <= 0:
            errors.append("INVALID_FRAME_DIMENSIONS")
            continue
        areas.append(min(1, w * h / (sw * sh)))
    if areas and min(areas) < 0.9999:
        warnings.append("INCOMPLETE_ORIGINAL_SCREEN_COVERAGE")

    segment_rows = subtitles.get("segments", []) or []
    if subtitles:
        if subtitles.get("valid_segment_count", len(segment_rows)) != len(segment_rows):
            errors.append("SUBTITLE_SEGMENT_COUNT_MISMATCH")
        seg_ids = [str(x.get("segment_id") or x.get("id") or "") for x in segment_rows]
        if len(seg_ids) != len(set(seg_ids)):
            errors.append("DUPLICATED_SUBTITLE_SEGMENT_ID")
    else:
        errors.append("SUBTITLE_SOURCE_UNAVAILABLE")

    ref_count = 0
    if manifest and analysis:
        try:
            _, ref_report = reconcile_analysis_evidence(analysis, manifest)
            ref_count = int(ref_report.get("validated_panel_refs", 0))
        except (InvalidEvidenceReference, ValueError) as exc:
            errors.append("INVALID_EVIDENCE_REF:" + str(exc)[:400])

    # A typed research item remains a model claim; require valid evidence.
    known_ids = {str(seg.get("id") or seg.get("segment_id")) for seg in segment_rows}
    for idx, item in enumerate(analysis.get("research_items", []) or [], 1):
        sec = item.get("section_id")
        if type(sec) is not int or not 1 <= sec <= 18:
            errors.append(f"INVALID_RESEARCH_SECTION:{idx}")
        if not str(item.get("text") or "").strip():
            errors.append(f"EMPTY_RESEARCH_CLAIM:{idx}")
        ids = item.get("source_segment_ids", []) or []
        if not isinstance(ids, list):
            errors.append(f"INVALID_RESEARCH_SEGMENT_LIST:{idx}")
            ids = []
        if any(str(sid) not in known_ids for sid in ids):
            errors.append(f"UNKNOWN_RESEARCH_SEGMENT:{idx}")
        if not item.get("evidence_ref") and not ids:
            errors.append(f"UNSOURCED_RESEARCH_CLAIM:{idx}")

    # Optional V3.5 detailed workflow and CLI evidence must be structurally grounded.
    for collection in ("workflow_steps", "cli_commands"):
        rows = analysis.get(collection, []) or []
        if not isinstance(rows, list):
            errors.append(f"INVALID_{collection.upper()}_ARRAY")
            continue
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                errors.append(f"INVALID_{collection.upper()}_ITEM:{index}")
                continue
            content_key = "action" if collection == "workflow_steps" else "command"
            if not str(row.get(content_key) or "").strip():
                errors.append(f"MISSING_{collection.upper()}_TEXT:{index}")
            ids = row.get("source_segment_ids", []) or []
            if not isinstance(ids, list) or any(str(i) not in known_ids for i in ids):
                errors.append(f"UNKNOWN_{collection.upper()}_SOURCE:{index}")
            if not row.get("evidence_ref") and not ids:
                errors.append(f"UNSOURCED_{collection.upper()}_ITEM:{index}")
            if collection == "workflow_steps" and row.get("interaction_type") not in (
                "MANUAL_UI", "PRODUCT_GENERATED", "CLI_EXECUTION",
            ):
                errors.append(f"UNCLASSIFIED_WORKFLOW_INTERACTION:{index}")

    # Deterministic claim audit does not assert evidence/statement semantic truth.
    try:
        claim_review = summarize_claim_reviews(analysis, manifest) if manifest else {
            "claims_total": 0, "items": [], "flag_counts": {},
            "independent_semantic_checks_completed": 0,
        }
    except (ValueError, TypeError) as exc:
        errors.append("INVALID_RESEARCH_CLAIM_PROVENANCE:" + str(exc)[:180])
        claim_review = {
            "claims_total": len(analysis.get("research_items", []) or []),
            "items": [], "flag_counts": {},
            "independent_semantic_checks_completed": 0,
            "review_required": True,
        }
    for flag, count in claim_review.get("flag_counts", {}).items():
        # Do not create nine high-priority warnings merely because the claims
        # are legacy/unverified or reference multiple separate timestamps.
        # Risk warnings reflect only a specific reviewable content signal.
        if flag in {
            "SERVICE_QUOTA_UNVERIFIED", "UNTESTED_WORKAROUND",
            "MIXED_CLAIMS_REQUIRE_SPLIT", "MODEL_VERSION_ATTRIBUTION_UNVERIFIED",
            "ARCHITECTURE_OR_ACCURACY_UNVERIFIED", "WORKFLOW_STEPS_UNVERIFIED",
            "ALL_CITED_PANELS_TEMPORALLY_DISTANT",
        }:
            warnings.append(f"RESEARCH_CLAIM_RISK:{flag}:{count}")

    # Manual audit dispositions remain separate from rule-based triage:
    # a P0 review signal is not a confirmed error, and a confirmed source
    # conflict is not automatically repaired by a structural PASS.
    try:
        audited_claims = summarize_audited_claims(
            root, video_id, run_id, analysis=analysis, frames=manifest,
        )
    except (ClaimAuditInvalid, OSError, ValueError, TypeError) as exc:
        errors.append("CLAIM_ADJUDICATION_STALE_OR_INVALID:" + str(exc)[:180])
        audited_claims = {
            "audit_id": None, "reviewed_claims": 0,
            "claims_total": len(analysis.get("research_items", []) or []),
            "status_counts": {
                "CONFIRMED_ERROR": 0, "SOURCE_CONFLICT": 0, "UNVERIFIED": 0,
                "NOT_REVIEWED": len(analysis.get("research_items", []) or []),
            },
            "has_unresolved_confirmed_problem": False,
            "items": [],
        }
    claim_remediation = None
    if audited_claims.get("audit_id"):
        try:
            claim_remediation = summarize_claim_remediations(root, video_id, run_id)
        except (ClaimRemediationInvalid, ClaimAuditInvalid, OSError, TypeError, ValueError) as exc:
            errors.append("CLAIM_REMEDIATION_LEDGER_INVALID:" + str(exc)[:180])
    counts = audited_claims["status_counts"]
    if counts["CONFIRMED_ERROR"]:
        warnings.append(f"AUDIT_CONFIRMED_ERROR_REMEDIATION_REQUIRED:{counts['CONFIRMED_ERROR']}")
    if counts["SOURCE_CONFLICT"]:
        warnings.append(f"AUDIT_SOURCE_CONFLICT_REMEDIATION_REQUIRED:{counts['SOURCE_CONFLICT']}")

    chapters = analysis.get("scene_analyses", []) or []
    model_statuses: Dict[str, int] = {}
    for item in verification.get("items", []) or []:
        status = str(item.get("final_status", "UNVERIFIED")).upper()
        model_statuses[status] = model_statuses.get(status, 0) + 1
        if status == "VERIFIED":
            errors.append("AMBIGUOUS_LEGACY_VERIFIED_STATUS")
        if status == "INDEPENDENT_VERIFIED":
            # Self-reported independent status is not sufficient without a
            # reviewer identity and actual review provenance.
            if not item.get("independent_verifier_invoked") or not item.get("reviewer_id"):
                errors.append("UNPROVEN_INDEPENDENT_VERIFIED_STATUS")
        if item.get("ground_truth") is not None and not item.get("ground_truth_source"):
            errors.append("MODEL_GROUND_TRUTH_WITHOUT_INDEPENDENT_PROVENANCE")
    if not chapters:
        errors.append("NO_ANALYSIS_CHAPTERS")
    if chapters and not verification.get("items"):
        errors.append("MISSING_VERIFICATION_ITEMS")
    if chapters and verification.get("items") and len(verification["items"]) != len(chapters):
        warnings.append("ANALYSIS_VERIFICATION_ITEM_COUNT_DIFFERS")

    # Missing literal prompts and numbers are high-value audit candidates, not
    # proof of an incorrect model inference; no invented OCR accuracy score.
    prompts = analysis.get("prompts_extracted", []) or []
    ungrounded_prompts = sum(not x.get("evidence_ref") for x in prompts)
    if ungrounded_prompts:
        warnings.append(f"UNREFERENCED_SCREEN_TEXT_ITEMS:{ungrounded_prompts}")
    for item in prompts:
        kind = str(item.get("evidence_kind", "SCREEN_TEXT_UNSPECIFIED")).upper()
        text = str(item.get("verbatim_text", ""))
        if kind == "GENERATED_CONTENT" and (
            re.search(r"\d+(?:\.\d+)?\s*%", text) or
            any(token in text for token in ("항상", "절대", "100%", "무조건"))
        ):
            observations.append({
                "code": "SOURCE_CLAIM_UNVERIFIED",
                "evidence_ref": item.get("evidence_ref"),
                "note": "Visible generated-content claim; independent source verification not performed.",
            })

    times = sorted(float(f.get("actual_sec", f.get("requested_sec", 0))) for f in frames)
    max_gap = max((b-a for a, b in zip(times, times[1:])), default=0)
    if max_gap > 120:
        warnings.append("SPARSE_TEMPORAL_COVERAGE_REVIEW")
    if not errors:
        structural_status = "PASS"
        approval_status = (
            "NOT_APPROVABLE" if audited_claims["has_unresolved_confirmed_problem"]
            else "REVIEW_REQUIRED"
        )
    else:
        structural_status = "BLOCKED"
        approval_status = "NOT_APPROVABLE"
    result = {
        "schema_version": 1,
        "video_id": video_id,
        "run_id": run_id,
        "structural_status": structural_status,
        "approval_status": approval_status,
        "human_review_required": True,
        "independent_content_accuracy_measured": False,
        "errors": errors,
        "warnings": warnings,
        "generated_claim_observations": observations,
        "claim_review": claim_review,
        "claim_adjudications": audited_claims,
        "claim_remediation": claim_remediation,
        "semantic_evidence_status": (
            "AUDIT_REPORTED_REMEDIATION_REQUIRED"
            if audited_claims["has_unresolved_confirmed_problem"]
            else "REVIEW_REQUIRED"
        ),
        "metrics": {
            "frame_count": len(frames),
            "roi_frame_count": len(rois),
            "board_count": len(boards),
            "board_sha256_checked": board_hash_checked,
            "validated_evidence_panel_refs": ref_count,
            "screen_area_min_retained_pct": round(min(areas) * 100, 2) if areas else 0,
            "subtitle_segment_count": len(segment_rows),
            "max_keyframe_gap_sec": round(max_gap, 2),
            "analysis_chapter_count": len(chapters),
            "screen_text_items": len(prompts),
            "reported_model_statuses": model_statuses,
            "research_claims_pending_semantic_review": claim_review.get("claims_total", 0),
            "research_claim_priority_counts": claim_review.get(
                "priority_counts", {"P0": 0, "P1": 0, "P2": 0},
            ),
            "research_claims_unverified": claim_review.get("unverified_claims", 0),
            "audit_claim_status_counts": audited_claims["status_counts"],
            "audit_claims_reviewed": audited_claims["reviewed_claims"],
        },
        "limitation": (
            "Structural correctness and reference/hash checks do not prove text "
            "recognition accuracy or source factual truth. Human review required."
        ),
    }
    if persist:
        atomic_write(run_dir / "quality_gate.json", json_text(result))
    return result
