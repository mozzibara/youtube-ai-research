"""Conservative provenance assessment for research claims, not an AI fact checker.

Grounds cited board PANEL numbers and subtitle IDs in persisted manifests.
A syntactically valid citation is NOT a semantic content match, proof of a
model version, benchmark, pricing policy, or successful workflow execution.

For old V3.4 research_items without claim_origin we retain the claim with a
prominent LEGACY_UNATTRIBUTED label and independent review requirement. We
never silently relabel prior AI-generated content as witnessed fact.
"""
from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .evidence_refs import BOARD_REF_RE, PANEL_RE, build_panel_catalog


CLAIM_ORIGINS = frozenset({
    "SPEAKER_STATEMENT",
    "OBSERVED_UI",
    "OBSERVED_USER_ACTION",
    "AI_GENERATED_OUTPUT",
    "GENERATED_SLIDE",
    "SOURCE_DOCUMENT",
    "LEGACY_UNATTRIBUTED",
    "MIXED_SOURCES",
})
NON_FACT_SOURCES = frozenset({
    "AI_GENERATED_OUTPUT", "GENERATED_SLIDE", "SPEAKER_STATEMENT",
    "LEGACY_UNATTRIBUTED", "MIXED_SOURCES",
})
# Priority is assigned ONLY from a concrete content/action risk signal.
# Being unverified or legacy is a verification state, not evidence of an error.
# P0 = potentially consequential actionable misinformation; NOT a confirmed error.
# P1 = targeted human review; P2 = normal unverified/source-type review.
CLAIM_SEVERITY = {
    "SERVICE_QUOTA_UNVERIFIED": "P0",
    "UNTESTED_WORKAROUND": "P0",
    "MIXED_CLAIMS_REQUIRE_SPLIT": "P1",
    "MODEL_VERSION_ATTRIBUTION_UNVERIFIED": "P1",
    "ARCHITECTURE_OR_ACCURACY_UNVERIFIED": "P1",
    "WORKFLOW_STEPS_UNVERIFIED": "P1",
    "ALL_CITED_PANELS_TEMPORALLY_DISTANT": "P1",
}
VERIFICATION_GAP_FLAGS = frozenset({
    "SOURCE_CLAIM_UNVERIFIED",
    "LEGACY_SOURCE_TYPE_UNATTRIBUTED",
    "EVIDENCE_CONTENT_MATCH_NOT_REVIEWED",
})
CONTEXT_FLAGS = frozenset({"CROSS_TIME_COMPOSITE_REFERENCE"})

_QUOTA_PATTERN = re.compile(
    r"(?:\d[\d,]*(?:\.\d+)?\s*(?:개|회|번|GB|MB|단어|%)|"
    r"(?:월간|일일|무료|유료|플랜|한도|제공|요금제))",
    re.IGNORECASE,
)
_PRICE_PATTERN = re.compile(
    r"노트북.{0,30}(?:\d[\d,]*)|"
    r"(?:무료|유료|플랜|plus).{0,70}(?:\d[\d,]*)",
    re.IGNORECASE,
)
# Avoid flagging a harmless literal number or an unrelated use of "우회".
# Require a quota context AND a concrete split/upload or bypass action.
_WORKAROUND_QUOTA = re.compile(
    r"(?:499,?999|(?:50|\d{2,})\s*만\s*단어|파일\s*(?:용량\s*)?한도)", re.I,
)
_WORKAROUND_CONTEXT = re.compile(
    r"(?:코드\s*(?:레포지토리|리포지토리|저장소|베이스)|"
    r"레포지토리|리포지토리|파일(?:\s*조각|로|을|를)?|\.txt|텍스트\s*파일|소스\s*파일)", re.I,
)
_WORKAROUND_ACTION = re.compile(r"(?:분할|조각|쪼개|업로드|우회|회피|넘기)", re.I)
_MODELS_PATTERN = re.compile(r"(?:gemini\s*[\d.]+|nano\s*banana|사용\s*모델|구동\s*모델)", re.I)
_ARCH_PATTERN = re.compile(
    r"(?:RAG|retrieval.augmented|환각|hallucinat|"
    r"소스에만|엄격하게\s*참조|모든\s*답변|정확도|성능.{0,20}향상)",
    re.I,
)
_WORKFLOW_PATTERN = re.compile(r"(?:원클릭|1.click|한\s*번의\s*클릭|일괄.{0,35}생성|자동화)", re.I)


def _time_seconds(value: Any) -> Optional[float]:
    if isinstance(value, (float, int)):
        return float(value)
    if not value:
        return None
    parts = str(value).split(":")
    try:
        if not 1 <= len(parts) <= 3:
            return None
        return sum(float(v) * 60 ** i for i, v in enumerate(reversed(parts)))
    except ValueError:
        return None


def board_timestamps_for_ref(
    evidence_ref: str, manifest: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Resolve board and panel timestamps, without asserting what they depict."""
    catalog = build_panel_catalog(manifest)
    results = []
    for match in BOARD_REF_RE.finditer(str(evidence_ref or "")):
        name = match.group("name")
        board = catalog.get(name)
        if not board:
            continue
        for panel in PANEL_RE.findall(match.group("panels")):
            info = board["panels"].get(int(panel))
            if info is not None:
                results.append({
                    "board": name,
                    "panel": int(panel),
                    "frame_id": info["frame_id"],
                    "timestamp_sec": info.get("timestamp_sec"),
                })
    return results


def review_research_claim(
    claim: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    max_time_offset_sec: float = 90.0,
) -> Dict[str, Any]:
    """Assess attribution and review risk, NOT semantic correctness."""
    raw_origin = claim.get("claim_origin")
    origin = str(raw_origin).upper() if raw_origin else "LEGACY_UNATTRIBUTED"
    if origin not in CLAIM_ORIGINS:
        raise ValueError("INVALID_CLAIM_ORIGIN:" + origin)
    text = str(claim.get("text") or "")
    section = claim.get("section_id")
    flags: List[str] = []

    if origin == "LEGACY_UNATTRIBUTED":
        flags.append("LEGACY_SOURCE_TYPE_UNATTRIBUTED")
    if origin == "MIXED_SOURCES":
        flags.append("MIXED_CLAIMS_REQUIRE_SPLIT")
    if origin in NON_FACT_SOURCES:
        flags.append("SOURCE_CLAIM_UNVERIFIED")

    # These are review triggers, not a claim that a quota or method is false.
    if (section == 15 and _QUOTA_PATTERN.search(text) and
        (_PRICE_PATTERN.search(text) or "NotebookLM" in text)):
        flags.append("SERVICE_QUOTA_UNVERIFIED")
    if (
        _WORKAROUND_QUOTA.search(text)
        and _WORKAROUND_CONTEXT.search(text)
        and _WORKAROUND_ACTION.search(text)
    ):
        flags.append("UNTESTED_WORKAROUND")
    if (section == 2 and _MODELS_PATTERN.search(text)):
        flags.append("MODEL_VERSION_ATTRIBUTION_UNVERIFIED")
    if section in (9, 16) and _ARCH_PATTERN.search(text):
        flags.append("ARCHITECTURE_OR_ACCURACY_UNVERIFIED")
    if section == 14 and _WORKFLOW_PATTERN.search(text):
        flags.append("WORKFLOW_STEPS_UNVERIFIED")

    frames = board_timestamps_for_ref(str(claim.get("evidence_ref") or ""), manifest)
    claim_sec = _time_seconds(claim.get("timestamp"))
    distant = []
    if claim_sec is not None:
        for f in frames:
            ts = f.get("timestamp_sec")
            if isinstance(ts, (float, int)) and abs(claim_sec - ts) > max_time_offset_sec:
                distant.append(f["board"] + " PANEL #" + f"{f['panel']:02d}")
    if distant:
        # A broad multi-scene summary often intentionally cites many moments.
        # Distant *supplementary* panels alone are informational, not an error.
        flags.append("CROSS_TIME_COMPOSITE_REFERENCE")
    if frames and claim_sec is not None and len(distant) == len(frames):
        # No cited image is close to the alleged timestamp: targeted review.
        flags.append("ALL_CITED_PANELS_TEMPORALLY_DISTANT")

    # Citation validity is not an independent claim-to-image semantic judgment.
    flags.append("EVIDENCE_CONTENT_MATCH_NOT_REVIEWED")
    flags = list(dict.fromkeys(flags))
    risk_signals = [flag for flag in flags if flag in CLAIM_SEVERITY]
    verification_gaps = [flag for flag in flags if flag in VERIFICATION_GAP_FLAGS]
    context_flags = [flag for flag in flags if flag in CONTEXT_FLAGS]
    priority = (
        "P0" if any(CLAIM_SEVERITY[flag] == "P0" for flag in risk_signals) else
        "P1" if risk_signals else "P2"
    )
    review_class = {
        "P0": "ACTIONABLE_RISK_SIGNAL",
        "P1": "TARGETED_CONTENT_REVIEW",
        "P2": "BASELINE_UNVERIFIED_REVIEW",
    }[priority]
    return {
        "claim_origin": origin,
        "factual_verification_status": "UNVERIFIED",
        "evidence_reference_status": "STRUCTURALLY_VALID_ONLY",
        "evidence_content_match": "NOT_REVIEWED",
        "semantic_review_required": True,
        "review_priority": priority,
        "review_class": review_class,
        "risk_signals": risk_signals,
        "verification_gaps": verification_gaps,
        "context_flags": context_flags,
        "review_flags": flags,
        "distant_evidence_refs": distant,
        "board_evidence": frames,
        "attribution_note": (
            "Model/speaker/generated-content claims are quotations of the video, "
            "not independently verified technical facts."
            if origin in NON_FACT_SOURCES else
            "On-screen action/label observation is not independent confirmation "
            "of product implementation, performance or external factual claims."
        ),
    }


def review_cached_research_claims(
    root: Path, video_id: str, run_id: str,
    *, persist: bool = True,
) -> Dict[str, Any]:
    """Offline-only V3.4/V3.5 risk reprioritization.

    Reads the original analysis and frame manifest, stores ONLY a distinct
    sidecar. Never rewrites model output, evidence boards or research reports;
    does not invoke AGY, YouTube or any external service.
    """
    from .research_state import VIDEO_ID, RUN_ID
    from .storage import read_json, atomic_write, json_text
    if not VIDEO_ID.fullmatch(video_id) or not RUN_ID.fullmatch(run_id):
        raise ValueError("VIDEO_OR_RUN_ID_INVALID")
    folder = Path(root) / "data" / "videos" / video_id / "runs" / run_id
    analysis_file = folder / "tiered_analysis.json"
    manifest_file = folder / "frames_manifest.json"
    analysis_raw = analysis_file.read_bytes()
    manifest_raw = manifest_file.read_bytes()
    analysis = read_json(analysis_file)
    manifest = read_json(manifest_file)
    result = summarize_claim_reviews(analysis, manifest)
    result["video_id"] = video_id
    result["run_id"] = run_id
    result["source_analysis_sha256"] = hashlib.sha256(analysis_raw).hexdigest()
    result["source_frames_manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    result["provenance"] = "DETERMINISTIC_OFFLINE_RECLASSIFICATION"
    result["source_files_unchanged"] = True
    if persist:
        out = folder / "claim_review_risk_v2.json"
        atomic_write(out, json_text(result))
        result["output_path"] = str(out)
    return result


def summarize_claim_reviews(
    analysis: Mapping[str, Any], manifest: Mapping[str, Any]
) -> Dict[str, Any]:
    results = []
    for index, item in enumerate(analysis.get("research_items", []) or [], 1):
        detail = review_research_claim(item, manifest)
        results.append({
            "item_index": index,
            "section_id": item.get("section_id"),
            "timestamp": item.get("timestamp"),
            "evidence_ref": item.get("evidence_ref"),
            **detail,
        })
    flag_counts: Dict[str, int] = {}
    for row in results:
        for flag in row["review_flags"]:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    priority_counts = {
        priority: sum(r["review_priority"] == priority for r in results)
        for priority in ("P0", "P1", "P2")
    }
    return {
        "schema_version": 2,
        "claims_total": len(results),
        "review_required": bool(results),
        "independent_semantic_checks_completed": 0,
        "high_priority_claims": priority_counts["P0"],
        "priority_counts": priority_counts,
        "unverified_claims": sum(
            r["factual_verification_status"] == "UNVERIFIED" for r in results
        ),
        "claims_with_content_specific_risk": priority_counts["P0"] + priority_counts["P1"],
        "claims_only_general_review": priority_counts["P2"],
        "flag_counts": flag_counts,
        "items": results,
        "limitation": (
            "Citation ID/hash correctness and timestamp consistency do not "
            "establish that the cited image/subtitle supports the claim."
        ),
    }
