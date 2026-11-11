"""Deterministic validation of rendered Evidence Board panel references.

IMPORTANT: A frame_id (e.g. roi_center_001) is NOT a PANEL number.
The renderer assigns ROI panel numbers continuously across the combined
12 ROI frames. roi_board_03.png has panels #05 and #06, not #01 and #02.
Existing V3 references must be preserved as originally rendered.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Tuple


BOARD_REF_RE = re.compile(
    r"(?P<name>(?:roi_)?board_\d+\.png)\s*\((?P<panels>[^)]*)\)",
    re.IGNORECASE,
)
PANEL_RE = re.compile(r"PANEL\s*#\s*(\d+)", re.IGNORECASE)
BOARD_NAME_RE = re.compile(r"(?<!\w)(?:roi_)?board_\d+\.png", re.IGNORECASE)
FRAME_ID_RE = re.compile(r"_(\d+)$")


class InvalidEvidenceReference(ValueError):
    """An evidence reference has no exactly corresponding rendered panel."""


def build_panel_catalog(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """Derive the true printed PANEL number, NOT the frame_id suffix.

    New manifests explicitly record panel_indices; older manifests are
    reconstructed with the exact ordering used by build_visual_evidence_boards.
    Full boards use frame_001..N; ROI boards use a single continuous ROI series.
    """
    catalog: Dict[str, Any] = {}
    roi_render_position = 0
    for board in manifest.get("boards", []) or []:
        # JSON manifests contain Windows absolute paths even when reviewed on Linux.
        name = str(board.get("path") or board.get("board_id", "")).replace("\\", "/").rsplit("/", 1)[-1]
        if not name.endswith(".png"):
            name += ".png"
        if not name or name in catalog:
            raise InvalidEvidenceReference(f"Invalid or duplicate board: {name}")
        frame_ids = list(board.get("panel_frame_ids") or [])
        if not frame_ids:
            raise InvalidEvidenceReference(f"Missing panel_frame_ids: {name}")
        declared = board.get("panel_indices")
        if declared is not None and len(declared) != len(frame_ids):
            raise InvalidEvidenceReference(f"Invalid panel_indices length: {name}")
        panel_numbers: Dict[int, Dict[str, Any]] = {}
        for pos, frame_id in enumerate(frame_ids):
            frame_match = FRAME_ID_RE.search(str(frame_id))
            if not frame_match:
                raise InvalidEvidenceReference(f"Invalid frame_id: {name}/{frame_id}")
            is_roi = name.startswith("roi_board_")
            if is_roi:
                roi_render_position += 1
                rendered_index = roi_render_position
            else:
                rendered_index = int(frame_match.group(1))
            if declared is not None:
                actual = int(declared[pos])
                if actual != rendered_index:
                    raise InvalidEvidenceReference(
                        f"Manifest PANEL index conflicts with renderer order: "
                        f"{name} declared={actual} rendered={rendered_index}"
                    )
            if rendered_index in panel_numbers:
                raise InvalidEvidenceReference(f"Duplicate rendered panel {name}: #{rendered_index:02d}")
            times = board.get("timestamps_sec") or []
            panel_numbers[rendered_index] = {
                "frame_id": str(frame_id),
                "panel_index": rendered_index,
                "timestamp_sec": times[pos] if len(times) > pos else None,
            }
        catalog[name] = {"panels": panel_numbers}
    return catalog


def _validate_ref(
    value: str, catalog: Mapping[str, Any],
) -> Tuple[str, int]:
    if not value:
        return value, 0
    # Legacy structured evidence can refer to a single extracted frame by ID.
    # This is safe only if that exact frame exists on a manifest board.
    if re.fullmatch(r"(?:frame_\d+|roi_(?:left|center|right)_\d+)", value):
        known_frames = {
            meta["frame_id"]
            for board in catalog.values()
            for meta in board["panels"].values()
        }
        if value not in known_frames:
            raise InvalidEvidenceReference("Frame ID not present in manifest: " + value)
        return value, 1
    references = list(BOARD_REF_RE.finditer(value))
    unmatched = BOARD_NAME_RE.findall(BOARD_REF_RE.sub("", value))
    if unmatched:
        raise InvalidEvidenceReference(
            "Board mentioned without PANEL index: " + ", ".join(unmatched)
        )
    if not references:
        raise InvalidEvidenceReference("No parseable board/PANEL reference: " + value)
    validated = 0
    for ref in references:
        name = ref.group("name")
        if name not in catalog:
            raise InvalidEvidenceReference("Board not present in manifest: " + name)
        numbers = PANEL_RE.findall(ref.group("panels"))
        if not numbers:
            raise InvalidEvidenceReference("Missing PANEL number for " + name)
        if PANEL_RE.sub("", ref.group("panels")).strip(" ,;/"):
            raise InvalidEvidenceReference("Unparseable PANEL annotation in " + name)
        for number in numbers:
            n = int(number)
            if n not in catalog[name]["panels"]:
                raise InvalidEvidenceReference(
                    f"Nonexistent displayed panel: {name} PANEL #{n:02d}; "
                    f"valid panels: {sorted(catalog[name]['panels'])}"
                )
            validated += 1
    # Never silently change an evidence_ref: the existing V3 refs are correct.
    return value, validated


def reconcile_analysis_evidence(
    analysis: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    allow_legacy_global_roi: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validate every structured reference without guessing or altering it.

    The legacy parameter is retained for callers but never remaps panel IDs.
    A failed reference aborts the run before analysis persistence.
    """
    catalog = build_panel_catalog(manifest)
    result = copy.deepcopy(dict(analysis))
    errors: List[str] = []
    checked = 0
    for field in ("scene_analyses", "prompts_extracted", "ui_settings_extracted", "research_items", "workflow_steps", "cli_commands"):
        for index, item in enumerate(result.get(field, []) or [], start=1):
            raw = str(item.get("evidence_ref", "") or "")
            if not raw.strip():
                continue
            try:
                _unchanged, count = _validate_ref(raw, catalog)
                checked += count
            except InvalidEvidenceReference as exc:
                errors.append(f"{field}[{index}]: {exc}")
    if errors:
        raise InvalidEvidenceReference("; ".join(errors))
    return result, {
        "schema_version": 2,
        "all_refs_valid": True,
        "validated_panel_refs": checked,
        "corrected_panel_refs": 0,
        "corrections": [],
        "validation_basis": "RENDERED_BOARD_PANEL_INDICES",
    }
