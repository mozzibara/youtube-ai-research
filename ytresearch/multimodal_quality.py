"""Independent provenance and visual evidence quality utilities.

All helpers are deterministic. Primary-model assertions are never promoted to
ground truth or independent verification without an external review source.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Sequence


ROI_BOXES = {
    "left": (0.0, 0.0, 0.45, 1.0),
    "center": (0.28, 0.0, 0.75, 1.0),
    "right": (0.60, 0.0, 1.0, 1.0),
}
EVIDENCE_KINDS = (
    "USER_PROMPT",
    "AI_OUTPUT",
    "UI_PLACEHOLDER",
    "GENERATED_CONTENT",
    "SCREEN_TEXT_UNSPECIFIED",
)
VERIFICATION_STATES = (
    "SELF_CHECKED",
    "MODEL_CROSS_CHECKED",
    "INDEPENDENT_VERIFIED",
    "PARTIAL",
    "CONFLICT",
    "UNVERIFIED",
)


def resolve_chapter_source_units(
    aligned_timeline: Dict[str, Any],
    chapter: Dict[str, Any],
) -> Dict[str, Any]:
    """Join a logical analysis chapter to its source scenes by time, not overlapping IDs."""
    start = float(chapter.get("start_sec", 0))
    end = float(chapter.get("end_sec", start))
    selected = [
        u for u in aligned_timeline.get("units", [])
        if min(float(u["end_sec"]), end) - max(float(u["start_sec"]), start) > 0.001
    ]
    unique_subtitles = OrderedDict()
    frames = []
    for unit in selected:
        for sub in unit.get("subtitles", []):
            sid = sub.get("segment_id")
            if sid and sid not in unique_subtitles:
                unique_subtitles[sid] = sub
        frames.extend(unit.get("keyframes", []))
    return {
        "chapter_id": int(chapter.get("chapter_id", chapter.get("scene_id", 0))),
        "source_scene_ids": [int(u["scene_id"]) for u in selected],
        "start_sec": start,
        "end_sec": end,
        "keyframe_sec": float(selected[0].get("keyframe_sec", start)) if selected else start,
        "ui_density_score": max((float(u.get("ui_density_score", 0)) for u in selected), default=0.0),
        "is_ui_dense": any(bool(u.get("is_ui_dense")) for u in selected),
        "keyframes": frames,
        "subtitles": list(unique_subtitles.values()),
        "segment_ids": list(unique_subtitles),
        "combined_transcript_text": " ".join(str(s.get("text", "")) for s in unique_subtitles.values()),
    }


def attach_chapter_provenance(
    analysis_result: Dict[str, Any],
    aligned_timeline: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep legacy scene_id as a chapter alias but make its distinct domain explicit."""
    result = dict(analysis_result)
    scenes = []
    for item in result.get("scene_analyses", []):
        out = dict(item)
        match = resolve_chapter_source_units(aligned_timeline, item)
        out["chapter_id"] = match["chapter_id"]
        out["source_scene_ids"] = match["source_scene_ids"]
        out["evidence_segment_ids"] = match["segment_ids"]
        scenes.append(out)
    result["scene_analyses"] = scenes
    result["analysis_id_type"] = "LOGICAL_CHAPTER"
    result["source_id_type"] = "DETECTED_SCENE"
    return result


def classify_screen_text(item: Dict[str, Any]) -> Dict[str, Any]:
    """Classify without assuming every screen text is a user-authored prompt."""
    out = dict(item)
    label = str(item.get("label", "")).lower()
    raw = str(item.get("verbatim_text", ""))
    explicit = str(item.get("evidence_kind", "")).upper()
    label_prefix = label.split(":", 1)[0].strip().upper()
    if explicit in EVIDENCE_KINDS:
        kind = explicit
    elif label_prefix in EVIDENCE_KINDS:
        kind = label_prefix
    elif any(x in label for x in ("플레이스홀더", "placeholder", "입력 안내", "안내 문구")):
        kind = "UI_PLACEHOLDER"
    elif any(x in label for x in ("ai 응답", "자동 생성 요약", "추천 질문", "모델 응답", "답변", "챗봇 응답")):
        kind = "AI_OUTPUT"
    elif any(x in label for x in ("슬라이드", "마인드맵", "인포그래픽", "생성 결과", "생성된")):
        kind = "GENERATED_CONTENT"
    elif any(x in label for x in ("사용자 입력", "검색 쿼리", "검색어", "user prompt", "유저 프롬프트", "사용자 프롬프트")):
        kind = "USER_PROMPT"
    else:
        kind = "SCREEN_TEXT_UNSPECIFIED"
    out["evidence_kind"] = kind
    # Text from a single image/model cannot be certified complete without visual review.
    out["transcription_completeness"] = (
        "TRUNCATED_VISIBLE" if ("..." in raw or "…" in raw or "… " in label)
        else "UNCONFIRMED_COMPLETE"
    )
    out["human_review_required"] = True
    return out


def classify_evidence_items(analysis_result: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(analysis_result)
    result["prompts_extracted"] = [
        classify_screen_text(p) for p in result.get("prompts_extracted", []) or []
    ]
    return result


def build_provenance_verification_items(
    analysis_result: Dict[str, Any],
    *,
    primary_model: str,
) -> List[Dict[str, Any]]:
    """Never use the primary model's claims as their own ground truth."""
    cross_checked = set(analysis_result.get("argon_verified_scene_ids", []))
    has_independent_model = bool(
        analysis_result.get("argon_invoked")
        and "argon" not in primary_model.lower()
    )
    items = []
    for sc in analysis_result.get("scene_analyses", []) or []:
        sid = int(sc["scene_id"])
        own_status = str(sc.get("verification_status", "UNVERIFIED")).upper()
        if own_status in {"CONFLICT", "PARTIAL", "UNVERIFIED", "UNCERTAIN"}:
            final_status = "UNVERIFIED" if own_status == "UNCERTAIN" else own_status
        elif has_independent_model and sid in cross_checked:
            final_status = "MODEL_CROSS_CHECKED"
        else:
            final_status = "SELF_CHECKED"
        items.append({
            "item_id": sid,
            "chapter_id": int(sc.get("chapter_id", sid)),
            "source_scene_ids": list(sc.get("source_scene_ids", [])),
            "category": "logical_chapter",
            "timestamp": sc.get("start_sec", 0.0),
            "claim": sc.get("summary", ""),
            "primary_model_verdict": own_status,
            "primary_model": primary_model,
            "flash_verdict": own_status if "flash" in primary_model.lower() else "NOT_CALLED",
            "argon_verdict": (
                "MODEL_REVIEWED" if has_independent_model and sid in cross_checked
                else (own_status if "argon" in primary_model.lower() else "NOT_CALLED")
            ),
            "independent_verifier_invoked": bool(has_independent_model and sid in cross_checked),
            "verification_method": (
                "SEPARATE_MODEL_REVIEW" if has_independent_model and sid in cross_checked
                else "PRIMARY_MODEL_SELF_REPORT"
            ),
            "human_review_required": True,
            "final_status": final_status,
            "evidence_type": "MODEL_OUTPUT_WITH_REFERENCES",
            "evidence_location": str(sc.get("evidence_ref", "")),
            "ground_truth": None,
        })
    return items


def deduplicate_visual_frames(
    frames: Sequence[Dict[str, Any]],
    *,
    max_mean_difference: float = 0.001,
    preserve_after_seconds: float = 120.0,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Remove near-identical frames while preserving long-distance timeline coverage."""
    from PIL import Image  # type: ignore

    kept: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    signatures: List[tuple[List[int], float]] = []
    for frame in sorted(frames, key=lambda x: float(x.get("actual_sec", x.get("requested_sec", 0)))):
        path = Path(str(frame["path"]))
        with Image.open(path) as img:
            thumb = img.convert("L").resize((96, 54))
            pixels = list(thumb.getdata())
        ts = float(frame.get("actual_sec", frame.get("requested_sec", 0)))
        duplicate_of = None
        for (previous, prev_ts), retained in zip(signatures, kept):
            if ts - prev_ts > preserve_after_seconds:
                continue
            mean_delta = sum(abs(a - b) for a, b in zip(pixels, previous)) / (255.0 * len(pixels))
            if mean_delta <= max_mean_difference:
                duplicate_of = retained["frame_id"]
                break
        if duplicate_of:
            rejected.append({
                "frame_id": frame.get("frame_id"),
                "duplicate_of": duplicate_of,
                "timestamp_sec": ts,
                "reason": "NEAR_IDENTICAL_PIXELS",
            })
        else:
            kept.append(frame)
            signatures.append((pixels, ts))
    return kept, rejected
