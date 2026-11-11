"""A single opt-in, resumable path from registered source to V3.x evidence.

Unlike the legacy 'run' command, this entrypoint cannot silently substitute a
transcript-only mock for a live AGY analysis. Tests inject a callable explicitly.
All analysis artifacts remain under ignored data/ and are never auto-approved.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .research_state import (
    VIDEO_ID, open_research_db, record_analysis_checkpoint,
    scan_local_runs, sync_research_sources,
)
from .multimodal import run_resumable_multimodal_pipeline
from .quality_gate import assess_research_run
from .research_report import build_research_report


class ResearchRunInputError(ValueError):
    pass


def _existing_file(path: Optional[Path], label: str) -> Optional[Path]:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ResearchRunInputError(f"INPUT_MISSING_{label}: {resolved.name}")
    return resolved


def run_research_video(
    root: Path,
    video_id: str,
    *,
    video_file: Path,
    subtitle_json3: Path,
    highres_video: Optional[Path] = None,
    audio_file: Optional[Path] = None,
    primary_model: str = "auto",
    run_id: Optional[str] = None,
    max_keyframes: int = 32,
    roi_mode: str = "auto",
    use_live_agy: bool = False,
    use_argon_verification: bool = False,
    analyzer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    force: bool = False,
    expected_sources: int = 77,
) -> Dict[str, Any]:
    """Execute only registered source ID and record real checkpoint artifacts.

    AGY account calls require explicit use_live_agy=True. The only offline
    alternative is a caller-injected analyzer, intended for CI fixtures.
    """
    root = Path(root).resolve()
    if not VIDEO_ID.fullmatch(str(video_id)):
        raise ResearchRunInputError("VIDEO_ID_INVALID")
    if primary_model not in ("auto", "flash", "argon"):
        raise ResearchRunInputError("PRIMARY_MODEL_INVALID")
    if max_keyframes < 1 or max_keyframes > 96:
        raise ResearchRunInputError("MAX_KEYFRAMES_OUT_OF_RANGE")
    if use_live_agy and analyzer is not None:
        raise ResearchRunInputError("AMBIGUOUS_LIVE_AND_MOCK_ANALYZER")
    if not use_live_agy and analyzer is None:
        raise ResearchRunInputError("LIVE_AGY_EXPLICIT_OPT_IN_REQUIRED")
    if use_argon_verification and not use_live_agy:
        raise ResearchRunInputError("SECOND_MODEL_REQUIRES_LIVE_AGY")

    # Validate all input paths BEFORE importing/mutating the execution ledger.
    vid = _existing_file(video_file, "VIDEO")
    sub = _existing_file(subtitle_json3, "JSON3")
    highres = _existing_file(highres_video, "HIGHRES_VIDEO")
    audio = _existing_file(audio_file, "AUDIO")

    synced = sync_research_sources(root, expected=expected_sources)
    with closing(open_research_db(root)) as db:
        source_rows = db.execute(
            "SELECT source_id FROM source_entries WHERE video_id=? ORDER BY source_position",
            (video_id,),
        ).fetchall()
    if not source_rows:
        raise ResearchRunInputError("VIDEO_NOT_IN_REGISTERED_SOURCES")
    source_ids = [x["source_id"] for x in source_rows]

    try:
        result = run_resumable_multimodal_pipeline(
            root,
            video_id,
            video_path=vid,
            subtitle_json3_path=sub,
            highres_video_path=highres,
            audio_path=audio,
            primary_model=primary_model,
            run_id=run_id,
            max_keyframes=max_keyframes,
            roi_mode=roi_mode,
            flash_analyzer=analyzer,
            use_agy_flash=use_live_agy,
            use_agy_argon=bool(use_argon_verification and use_live_agy),
            force=force,
            copy_files=True,
        )
    except Exception:
        # Capture any newly written/failed checkpoints without retrying AGY.
        scan_local_runs(root)
        raise

    status = record_analysis_checkpoint(root, video_id, result["run_id"])
    if status["status"] != "COMPLETED":
        raise RuntimeError(f"RUN_CHECKPOINT_NOT_COMPLETE: {status['status']}")
    quality = assess_research_run(root, video_id, result["run_id"])
    # Only structurally valid runs produce an unapproved research draft.
    research_manifest = (
        build_research_report(root, video_id, result["run_id"])
        if quality["structural_status"] == "PASS" else None
    )
    return {
        "source_ids": source_ids,
        "video_id": video_id,
        "run_id": result["run_id"],
        "run_status": status["status"],
        "model": result["primary_model"],
        "checkpoint_path": result["checkpoint_path"],
        "evidence_report_path": result["report_path"],
        "analysis_path": result["analysis_path"],
        "verification_path": result["verification_path"],
        "report_review_state": "REVIEW_REQUIRED",
        "source_entry_total": synced["source_entries"],
        "quality_status": quality["structural_status"],
        "quality_gate_path": str(Path(result["checkpoint_path"]).parent / "quality_gate.json"),
        "quality_errors": quality["errors"],
        "research_report_path": (
            research_manifest["report_path"] if research_manifest else None
        ),
        "research_report_review_state": (
            research_manifest["review_state"] if research_manifest else "NOT_GENERATED"
        ),
        "note": "Analysis completed; evidence integrity/independent factual review remains separate.",
    }
