"""Validate explicit human claim-to-source review before 19-section approval.

This validates reviewer-record completeness and attachment integrity. It does
NOT prove the reviewer is independent or that source claims are objectively
true. A quoted AI-generated slide may be approved for publication ONLY when
retained as an unverified quote, never automatically as an official fact.

The approval record is a separate, manually authored run-scoped artifact.
The original model analysis and prior report bytes are not edited.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
from typing import Any, Dict

from .storage import read_json


class ClaimReviewRequired(ValueError):
    pass


def validate_human_claim_review(root: Path, video_id: str, run_id: str, *,
                                reviewer: str, report_sha256: str) -> Dict[str, Any]:
    run_dir = Path(root) / "data" / "videos" / video_id / "runs" / run_id
    evidence = read_json(run_dir / "tiered_analysis.json")
    claims = evidence.get("research_items", []) or []
    file = run_dir / "research_review_decisions.json"
    if not file.is_file():
        raise ClaimReviewRequired("HUMAN_SEMANTIC_REVIEW_RECORD_MISSING")
    doc = read_json(file)
    if doc.get("schema_version") != 1 or doc.get("video_id") != video_id or doc.get("run_id") != run_id:
        raise ClaimReviewRequired("REVIEW_DOCUMENT_ID_MISMATCH")
    if not reviewer or doc.get("reviewer") != reviewer:
        raise ClaimReviewRequired("REVIEWER_ID_MISMATCH")
    if doc.get("report_sha256") != report_sha256:
        raise ClaimReviewRequired("REVIEWED_REPORT_HASH_MISMATCH")
    try:
        checked_at = datetime.fromisoformat(str(doc["reviewed_at_utc"]).replace("Z", "+00:00"))
        if checked_at.tzinfo is None:
            raise ValueError("naive time")
    except (ValueError, KeyError, TypeError) as exc:
        raise ClaimReviewRequired("REVIEW_TIMESTAMP_INVALID") from exc
    decisions = doc.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(claims):
        raise ClaimReviewRequired("REVIEW_CLAIM_COUNT_MISMATCH")
    numbers = set()
    external_verified = 0
    for entry in decisions:
        if not isinstance(entry, dict):
            raise ClaimReviewRequired("REVIEW_DECISION_INVALID")
        index = entry.get("item_index")
        if type(index) is not int or not 1 <= index <= len(claims) or index in numbers:
            raise ClaimReviewRequired("REVIEW_CLAIM_INDEX_INVALID")
        numbers.add(index)
        original = claims[index - 1]
        digest = hashlib.sha256(str(original.get("text", "")).encode("utf-8")).hexdigest()
        if entry.get("claim_sha256") != digest:
            raise ClaimReviewRequired("REVIEW_CLAIM_TEXT_CHANGED")
        if str(entry.get("cited_evidence_ref") or "") != str(original.get("evidence_ref") or ""):
            raise ClaimReviewRequired("REVIEW_CITATION_CHANGED")
        if entry.get("source_content_match") != "MATCH":
            raise ClaimReviewRequired("EVIDENCE_CONTENT_MATCH_UNRESOLVED")
        if len(str(entry.get("reviewer_note") or "").strip()) < 15:
            raise ClaimReviewRequired("REVIEW_RATIONALE_REQUIRED")
        disposition = entry.get("disposition")
        if disposition not in ("QUOTE_AS_UNVERIFIED", "EXTERNALLY_FACT_CHECKED"):
            raise ClaimReviewRequired("REVIEW_DISPOSITION_INVALID")
        if disposition == "EXTERNALLY_FACT_CHECKED":
            if (not str(entry.get("external_source_url") or "").startswith("https://")
                or not entry.get("external_checked_at")):
                raise ClaimReviewRequired("EXTERNAL_FACT_EVIDENCE_MISSING")
            external_verified += 1
    return {
        "reviewer": reviewer,
        "reviewed_at_utc": doc["reviewed_at_utc"],
        "claims_reviewed": len(claims),
        "external_fact_checks_recorded": external_verified,
        "fact_checked_automatically": False,
        "note": (
            "Human attestation of source-content match is not a model truth "
            "claim or verified external factual conclusion."
        ),
    }
