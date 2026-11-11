"""Multimodal video/audio/JSON3 optimization pipeline with Flash-default and conditional Argon escalation.

Implements:
1. Raw video, audio, and JSON3 subtitle caching with SHA-256 verification.
2. Code-based deterministic JSON3 subtitle parsing (preserving tStartMs, dDurationMs, tEndMs).
3. Media metadata extraction via ffprobe with automatic PyAV (av) fallback.
4. Visual scene change detection and UI text/edge density scoring.
5. High-resolution keyframe and ROI crop extraction for dense UI intervals.
6. Time-axis alignment between detected video scenes/keyframes and JSON3 subtitle segments.
7. Gemini 3.8 Flash default multimodal analysis.
8. Selective re-analysis of uncertain or high-UI-density intervals.
9. Conditional Gemini 4 Argon invocation for unresolved uncertain/conflict intervals.
10. Structured verification storage with provenance and evidence links.
11. Code-based deterministic final Markdown report and subtitle table assembly.
12. Checkpoint-based crash recovery and stage resumption.
13. Per-run actual token usage extraction and persistence (preventing cache/turn double-counting).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .storage import atomic_write, digest, json_text, read_json, video_dir
from .evidence_refs import build_panel_catalog, reconcile_analysis_evidence
from .multimodal_quality import (
    ROI_BOXES, attach_chapter_provenance, build_provenance_verification_items,
    classify_evidence_items, deduplicate_visual_frames, resolve_chapter_source_units,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Formatting & Hashing Utilities
# ---------------------------------------------------------------------------

def format_mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def format_mmss_ms(ms: int) -> str:
    safe_ms = max(0, int(ms))
    total_sec = safe_ms // 1000
    rem_ms = safe_ms % 1000
    return f"{total_sec // 60:02d}:{total_sec % 60:02d}.{rem_ms:03d}"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# 1. Raw Media & Subtitle Caching
# ---------------------------------------------------------------------------

def media_cache_dir(root: Path, video_id: str) -> Path:
    path = video_dir(root, video_id) / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_cache_manifest_path(root: Path, video_id: str) -> Path:
    return video_dir(root, video_id) / "media_cache.json"


def cache_raw_media_bundle(
    root: Path,
    video_id: str,
    *,
    video_path: Optional[Path] = None,
    audio_path: Optional[Path] = None,
    subtitle_path: Optional[Path] = None,
    highres_video_path: Optional[Path] = None,
    copy_files: bool = True,
) -> Dict[str, Any]:
    """Cache raw video, audio, and subtitle files with SHA-256 integrity checks."""
    manifest_file = media_cache_manifest_path(root, video_id)
    existing: Dict[str, Any] = {}
    if manifest_file.exists():
        try:
            existing = read_json(manifest_file)
        except Exception:
            existing = {}

    cache_folder = media_cache_dir(root, video_id).resolve()
    assets: Dict[str, Any] = dict(existing.get("assets", {}))
    inputs = {
        "video": video_path,
        "highres_video": highres_video_path,
        "audio": audio_path,
        "subtitle_json3": subtitle_path,
    }
    any_manifest_change = not manifest_file.exists()

    for kind, src in inputs.items():
        if src is None:
            continue
        src_path = Path(src).resolve()
        if not src_path.exists():
            raise FileNotFoundError(f"MEDIA_CACHE_SOURCE_MISSING: {kind}={src_path}")
        src_hash = file_sha256(src_path)
        src_size = src_path.stat().st_size
        prev = assets.get(kind)
        canonical_target = (cache_folder / src_path.name).resolve()
        target_path = canonical_target if copy_files else src_path

        cache_hit = False
        if (
            prev
            and prev.get("sha256") == src_hash
            and prev.get("size_bytes") == src_size
            and Path(prev.get("cached_path", "")).exists()
        ):
            prev_cached = Path(prev["cached_path"]).resolve()
            if not copy_files or prev_cached.parent == cache_folder:
                cache_hit = True
                target_path = prev_cached
            else:
                if canonical_target != src_path:
                    if not (
                        canonical_target.exists()
                        and canonical_target.stat().st_size == src_size
                        and file_sha256(canonical_target) == src_hash
                    ):
                        shutil.copy2(src_path, canonical_target)
                cache_hit = True
                target_path = canonical_target
        elif copy_files:
            if target_path != src_path:
                if not (
                    target_path.exists()
                    and target_path.stat().st_size == src_size
                    and file_sha256(target_path) == src_hash
                ):
                    shutil.copy2(src_path, target_path)

        if (
            prev
            and prev.get("sha256") == src_hash
            and prev.get("size_bytes") == src_size
            and prev.get("cached_path") == str(target_path)
            and prev.get("cache_hit") is True
            and cache_hit is True
        ):
            assets[kind] = dict(prev)
        else:
            any_manifest_change = True
            assets[kind] = {
                "kind": kind,
                "filename": target_path.name,
                "cached_path": str(target_path),
                "size_bytes": src_size,
                "sha256": src_hash,
                "cache_hit": cache_hit,
                "updated_utc": now_iso(),
            }

    if not any_manifest_change and existing:
        return existing

    manifest = {
        "schema_version": 1,
        "video_id": video_id,
        "updated_utc": now_iso(),
        "assets": assets,
    }
    atomic_write(manifest_file, json_text(manifest))
    return manifest


def load_media_cache(root: Path, video_id: str) -> Optional[Dict[str, Any]]:
    manifest_file = media_cache_manifest_path(root, video_id)
    if not manifest_file.exists():
        return None
    return read_json(manifest_file)


# ---------------------------------------------------------------------------
# 2. Code-Based Deterministic JSON3 Subtitle Parsing
# ---------------------------------------------------------------------------

def parse_json3_subtitles(
    json3_source: Path | str | Dict[str, Any],
    *,
    video_id: str = "",
    language: str = "ko",
) -> Dict[str, Any]:
    """Deterministically parse YouTube JSON3 subtitle events into structured segments."""
    if isinstance(json3_source, Path):
        raw_bytes = json3_source.read_text(encoding="utf-8")
        data = json.loads(raw_bytes)
        source_sha256 = digest(raw_bytes)
    elif isinstance(json3_source, str):
        possible_path = Path(json3_source)
        if possible_path.exists() and possible_path.is_file():
            raw_bytes = possible_path.read_text(encoding="utf-8")
            data = json.loads(raw_bytes)
            source_sha256 = digest(raw_bytes)
        else:
            data = json.loads(json3_source)
            source_sha256 = digest(json3_source)
    elif isinstance(json3_source, dict):
        data = json3_source
        source_sha256 = digest(json_text(data))
    else:
        raise TypeError("json3_source must be a Path, JSON string, or dict")

    events = data.get("events", [])
    if not isinstance(events, list):
        raise ValueError("JSON3_INVALID_EVENTS: events must be a list")

    segments: List[Dict[str, Any]] = []
    skipped_empty_events = 0

    for event in events:
        if not isinstance(event, dict):
            skipped_empty_events += 1
            continue
        segs = event.get("segs")
        if not segs or not isinstance(segs, list):
            skipped_empty_events += 1
            continue
        raw_text = "".join(str(part.get("utf8", "")) for part in segs if isinstance(part, dict))
        cleaned = re.sub(r"\s+", " ", raw_text).strip()
        if not cleaned:
            skipped_empty_events += 1
            continue

        t_start_ms = int(event.get("tStartMs", 0) or 0)
        d_duration_ms = int(event.get("dDurationMs", 0) or 0)
        t_end_ms = t_start_ms + max(0, d_duration_ms)
        start_sec = round(t_start_ms / 1000.0, 3)
        duration_sec = round(max(0, d_duration_ms) / 1000.0, 3)
        end_sec = round(t_end_ms / 1000.0, 3)

        seg_idx = len(segments) + 1
        seg_id = f"s{seg_idx:06d}"
        segments.append({
            "id": seg_id,
            "segment_id": seg_id,
            "index": seg_idx,
            "t_start_ms": t_start_ms,
            "d_duration_ms": d_duration_ms,
            "t_end_ms": t_end_ms,
            "start": start_sec,
            "duration": duration_sec,
            "end": end_sec,
            "timestamp": format_mmss(start_sec),
            "timestamp_ms": format_mmss_ms(t_start_ms),
            "text": cleaned,
        })

    min_start_ms = segments[0]["t_start_ms"] if segments else 0
    max_end_ms = max((s["t_end_ms"] for s in segments), default=0)
    last_start_ms = segments[-1]["t_start_ms"] if segments else 0

    return {
        "schema_version": 1,
        "video_id": video_id,
        "language": language,
        "source_method": "youtube_json3_code_parser",
        "source_sha256": source_sha256,
        "raw_event_count": len(events),
        "skipped_empty_event_count": skipped_empty_events,
        "valid_segment_count": len(segments),
        "time_range_ms": {
            "first_start_ms": min_start_ms,
            "last_start_ms": last_start_ms,
            "last_end_ms": max_end_ms,
        },
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# 3. Media Metadata Extraction (ffprobe with PyAV fallback)
# ---------------------------------------------------------------------------

def probe_media_metadata(media_path: Path, *, compute_hash: bool = True) -> Dict[str, Any]:
    """Extract container and stream metadata via ffprobe or PyAV fallback."""
    path = Path(media_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"MEDIA_PROBE_FILE_NOT_FOUND: {path}")

    info: Dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": file_sha256(path) if compute_hash else None,
        "probe_backend": None,
        "format": None,
        "duration_sec": None,
        "bit_rate": None,
        "streams": [],
    }

    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin:
        try:
            proc = subprocess.run(
                [
                    ffprobe_bin,
                    "-v", "quiet",
                    "-print_format", "json",
                    "-show_format",
                    "-show_streams",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=True,
            )
            raw_probe = json.loads(proc.stdout)
            fmt = raw_probe.get("format", {}) or {}
            info["probe_backend"] = "ffprobe"
            info["format"] = fmt.get("format_name")
            info["duration_sec"] = float(fmt["duration"]) if fmt.get("duration") else None
            info["bit_rate"] = int(fmt["bit_rate"]) if fmt.get("bit_rate") else None
            streams_out = []
            for s in raw_probe.get("streams", []) or []:
                s_type = s.get("codec_type")
                s_item: Dict[str, Any] = {
                    "index": s.get("index"),
                    "type": s_type,
                    "codec_name": s.get("codec_name"),
                    "codec_tag": s.get("codec_tag_string"),
                    "duration_sec": float(s["duration"]) if s.get("duration") else info["duration_sec"],
                }
                if s_type == "video":
                    s_item["width"] = s.get("width")
                    s_item["height"] = s.get("height")
                    fps_str = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
                    if "/" in str(fps_str):
                        num, den = str(fps_str).split("/", 1)
                        s_item["fps"] = round(float(num) / float(den), 3) if float(den) != 0 else None
                    else:
                        s_item["fps"] = float(fps_str) if fps_str else None
                    s_item["pix_fmt"] = s.get("pix_fmt")
                elif s_type == "audio":
                    s_item["sample_rate"] = int(s["sample_rate"]) if s.get("sample_rate") else None
                    s_item["channels"] = s.get("channels")
                streams_out.append(s_item)
            info["streams"] = streams_out
            return info
        except Exception:
            pass

    # Fallback to PyAV (av)
    import av  # type: ignore

    container = av.open(str(path))
    try:
        info["probe_backend"] = "pyav"
        info["format"] = container.format.name
        info["duration_sec"] = round(float(container.duration / av.time_base), 6) if container.duration else None
        info["bit_rate"] = container.bit_rate
        streams_out = []
        for s in container.streams:
            s_type = s.type
            codec_tag = s.codec_context.codec_tag
            codec_name = s.codec_context.name
            # Normalize decoder wrapper names (e.g., libdav1d -> av1 when tag is av01)
            normalized_codec = "av1" if codec_tag == "av01" else codec_name
            s_item = {
                "index": s.index,
                "type": s_type,
                "codec_name": normalized_codec,
                "decoder_name": codec_name,
                "codec_tag": codec_tag,
                "duration_sec": round(float(s.duration * s.time_base), 6) if (s.duration and s.time_base) else info["duration_sec"],
            }
            if s_type == "video":
                s_item["width"] = s.codec_context.width
                s_item["height"] = s.codec_context.height
                s_item["fps"] = round(float(s.average_rate), 3) if s.average_rate else None
                s_item["pix_fmt"] = s.codec_context.pix_fmt
            elif s_type == "audio":
                s_item["sample_rate"] = s.codec_context.sample_rate
                s_item["channels"] = s.codec_context.channels
            streams_out.append(s_item)
        info["streams"] = streams_out
    finally:
        container.close()
    return info


# ---------------------------------------------------------------------------
# 4. Scene Change Detection & UI Density Estimation
# ---------------------------------------------------------------------------

@dataclass
class SceneInterval:
    scene_id: int
    start_sec: float
    end_sec: float
    keyframe_sec: float
    mad_score: float
    ui_density_score: float
    is_ui_dense: bool
    transition_reason: str = "scene_cut"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["start_mmss"] = format_mmss(self.start_sec)
        d["end_mmss"] = format_mmss(self.end_sec)
        d["keyframe_mmss"] = format_mmss(self.keyframe_sec)
        return d


def _compute_frame_signature(
    pil_image: Any,
    *,
    roi_box: Tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    grid_size: Tuple[int, int] = (160, 90),
) -> Tuple[List[int], float]:
    """Compute a grayscale pixel array and spatial UI edge density score on a downscaled ROI."""
    w, h = pil_image.size
    x1, y1 = int(max(0.0, min(1.0, roi_box[0])) * w), int(max(0.0, min(1.0, roi_box[1])) * h)
    x2, y2 = int(max(0.0, min(1.0, roi_box[2])) * w), int(max(0.0, min(1.0, roi_box[3])) * h)
    if x2 > x1 and y2 > y1:
        cropped = pil_image.crop((x1, y1, x2, y2))
    else:
        cropped = pil_image
    gray = cropped.convert("L").resize(grid_size)
    pixels = list(getattr(gray, "get_flattened_data", gray.getdata)())
    gw, gh = grid_size

    # Estimate UI/text edge density via horizontal high-contrast step counts
    # and flat background ratio (to distinguish digital UI screens from full-screen camera shots)
    edge_hits = 0
    flat_hits = 0
    total_pairs = 0
    for row in range(gh):
        row_offset = row * gw
        for col in range(gw - 1):
            diff = abs(pixels[row_offset + col + 1] - pixels[row_offset + col])
            if diff <= 2:
                flat_hits += 1
            if diff >= 28:
                edge_hits += 1
            total_pairs += 1
    flat_ratio = flat_hits / max(1, total_pairs)
    # Full-screen camera/talking-head shots have low flat_ratio (< 0.50); penalize UI density
    raw_edge_density = edge_hits / max(1, total_pairs)
    ui_density = round(raw_edge_density if flat_ratio >= 0.50 else raw_edge_density * 0.25, 4)
    return pixels, ui_density


def detect_scene_transitions(
    video_path: Path,
    *,
    sample_interval_sec: float = 2.0,
    mad_threshold: float = 8.0,
    min_scene_duration_sec: float = 4.0,
    max_scene_duration_sec: float = 30.0,
    ui_density_threshold: float = 0.045,
    roi_box: Tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
) -> List[SceneInterval]:
    """Detect visual scene transitions and UI-dense intervals without video-specific hardcoding."""
    import av  # type: ignore

    path = Path(video_path).resolve()
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        time_base = float(stream.time_base)
        duration_sec = float(stream.duration * stream.time_base) if (stream.duration and stream.time_base) else (
            float(container.duration / av.time_base) if container.duration else 0.0
        )

        sampled: List[Tuple[float, List[int], float, float]] = []
        next_sample_ts = 0.0
        prev_pixels: Optional[List[int]] = None

        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            ts = float(frame.pts * time_base)
            if ts + 1e-3 < next_sample_ts:
                continue
            img = frame.to_image()
            pixels, ui_density = _compute_frame_signature(img, roi_box=roi_box)
            if prev_pixels is None:
                mad = 0.0
            else:
                mad = sum(abs(a - b) for a, b in zip(pixels, prev_pixels)) / max(1, len(pixels))
            sampled.append((round(ts, 3), pixels, round(mad, 3), ui_density))
            prev_pixels = pixels
            next_sample_ts = ts + sample_interval_sec
    finally:
        container.close()

    if not sampled:
        return []

    total_end = round(max(duration_sec, sampled[-1][0] + sample_interval_sec), 3)
    cut_indices: List[Tuple[int, str]] = [(0, "initial")]

    for idx in range(1, len(sampled)):
        ts, _pix, mad, _ui = sampled[idx]
        last_cut_ts = sampled[cut_indices[-1][0]][0]
        elapsed = ts - last_cut_ts
        if mad >= mad_threshold and elapsed >= min_scene_duration_sec:
            cut_indices.append((idx, "scene_cut"))
        elif elapsed >= max_scene_duration_sec:
            cut_indices.append((idx, "max_interval_split"))

    intervals: List[SceneInterval] = []
    for i, (start_idx, reason) in enumerate(cut_indices):
        end_idx = cut_indices[i + 1][0] if (i + 1) < len(cut_indices) else len(sampled)
        slice_samples = sampled[start_idx:end_idx]
        start_sec = slice_samples[0][0]
        end_sec = sampled[end_idx][0] if end_idx < len(sampled) else total_end

        # Pick a settled post-transition keyframe (prefer the highest UI density frame in the second half of the scene)
        candidates = slice_samples[1:] if len(slice_samples) > 1 else slice_samples
        best_sample = max(candidates, key=lambda item: item[3])
        max_mad = max(item[2] for item in slice_samples)
        avg_ui = round(sum(item[3] for item in slice_samples) / len(slice_samples), 4)
        peak_ui = max(item[3] for item in slice_samples)

        intervals.append(
            SceneInterval(
                scene_id=i + 1,
                start_sec=round(start_sec, 3),
                end_sec=round(end_sec, 3),
                keyframe_sec=round(best_sample[0], 3),
                mad_score=round(max_mad, 3),
                ui_density_score=round(peak_ui, 4),
                is_ui_dense=bool(peak_ui >= ui_density_threshold or avg_ui >= ui_density_threshold),
                transition_reason=reason,
            )
        )
    # Relative threshold prevents all scenes being labeled UI-dense merely
    # because every scene has text edges in a browser recording.
    if len(intervals) >= 4:
        ranked = sorted(x.ui_density_score for x in intervals)
        relative_threshold = ranked[int(0.65 * (len(ranked) - 1))]
        effective_threshold = max(ui_density_threshold, relative_threshold)
        for interval in intervals:
            interval.is_ui_dense = bool(interval.ui_density_score >= effective_threshold)
    return intervals


# ---------------------------------------------------------------------------
# 5. High-Resolution Frame Extraction for Important UI Intervals
# ---------------------------------------------------------------------------

def extract_highres_ui_frames(
    video_path: Path,
    output_dir: Path,
    timestamps_sec: Sequence[float],
    *,
    crop_box: Optional[Tuple[float, float, float, float]] = None,
    prefix: str = "frame",
) -> List[Dict[str, Any]]:
    """Extract high-resolution PNG frames (and optional ROI crops) at target timestamps."""
    import av  # type: ignore

    path = Path(video_path).resolve()
    out_folder = Path(output_dir).resolve()
    out_folder.mkdir(parents=True, exist_ok=True)

    extracted: List[Dict[str, Any]] = []
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        time_base = float(stream.time_base)

        for idx, target_ts in enumerate(sorted(set(round(float(t), 3) for t in timestamps_sec)), start=1):
            target_pts = max(0, int(target_ts / time_base))
            container.seek(target_pts, backward=True, any_frame=False, stream=stream)
            for frame in container.decode(stream):
                if frame.pts is None:
                    continue
                f_ts = float(frame.pts * time_base)
                if f_ts >= target_ts - 0.06:
                    img = frame.to_image()
                    orig_w, orig_h = img.size
                    if crop_box is not None:
                        x1 = int(max(0.0, min(1.0, crop_box[0])) * orig_w)
                        y1 = int(max(0.0, min(1.0, crop_box[1])) * orig_h)
                        x2 = int(max(0.0, min(1.0, crop_box[2])) * orig_w)
                        y2 = int(max(0.0, min(1.0, crop_box[3])) * orig_h)
                        if x2 > x1 and y2 > y1:
                            img = img.crop((x1, y1, x2, y2))
                    out_name = f"{prefix}_{idx:03d}_{f_ts:07.2f}s.png"
                    out_file = out_folder / out_name
                    img.save(out_file)
                    extracted.append({
                        "frame_id": f"{prefix}_{idx:03d}",
                        "requested_sec": target_ts,
                        "actual_sec": round(f_ts, 3),
                        "timestamp_mmss": format_mmss(f_ts),
                        "source_width": orig_w,
                        "source_height": orig_h,
                        "width": img.size[0],
                        "height": img.size[1],
                        "crop_box": list(crop_box) if crop_box else None,
                        "path": str(out_file),
                        "sha256": file_sha256(out_file),
                    })
                    break
    finally:
        container.close()
    return extracted


def build_visual_evidence_boards(
    frames: Sequence[Dict[str, Any]],
    output_dir: Path,
    *,
    panels_per_board: int = 2,
    max_panel_width: int = 3840,
    prefix: str = "board",
) -> List[Dict[str, Any]]:
    """Pack high-resolution UI keyframes/crops into annotated 2-panel inspection boards.

    Stacking 2 high-res UI panels per board preserves native 4K text legibility while
    reducing tool calls by 50% and allowing single-turn parallel `view_file` inspection.
    """
    from PIL import Image, ImageDraw  # type: ignore

    out_folder = Path(output_dir).resolve()
    out_folder.mkdir(parents=True, exist_ok=True)
    boards: List[Dict[str, Any]] = []
    frame_list = [f for f in frames if f.get("path") and Path(str(f["path"])).exists()]
    if not frame_list:
        return boards

    step = max(1, int(panels_per_board))
    header_h = 44
    gap_h = 10

    for b_idx, start_i in enumerate(range(0, len(frame_list), step), start=1):
        batch = frame_list[start_i:start_i + step]
        rendered_panels: List[Tuple[Any, str, Dict[str, Any]]] = []
        board_w = 0

        for p_offset, f_meta in enumerate(batch, start=start_i + 1):
            img = Image.open(str(f_meta["path"])).convert("RGB")
            w, h = img.size
            if w > max_panel_width:
                scale = max_panel_width / float(w)
                w = max_panel_width
                h = max(1, int(round(h * scale)))
                img = img.resize((w, h), Image.Resampling.LANCZOS)
            board_w = max(board_w, w)
            ts_sec = float(f_meta.get("actual_sec", f_meta.get("requested_sec", 0.0)))
            ts_mmss = f_meta.get("timestamp_mmss") or format_mmss(ts_sec)
            src_w = f_meta.get("source_width", w)
            src_h = f_meta.get("source_height", h)
            label_extra = f" | {f_meta['label']}" if f_meta.get("label") else ""
            banner_text = (
                f"PANEL #{p_offset:02d} | {ts_mmss} ({ts_sec:06.2f}s) | "
                f"Source {src_w}x{src_h} -> Crop {f_meta.get('width', w)}x{f_meta.get('height', h)}{label_extra}"
            )
            rendered_panels.append((img, banner_text, f_meta))

        total_h = sum(im.size[1] + header_h for im, _, _ in rendered_panels) + gap_h * max(0, len(rendered_panels) - 1)
        canvas = Image.new("RGB", (board_w, total_h), color=(18, 20, 28))
        draw = ImageDraw.Draw(canvas)

        curr_y = 0
        for im, banner_text, _f_meta in rendered_panels:
            draw.rectangle([0, curr_y, board_w, curr_y + header_h], fill=(28, 32, 48))
            draw.text((16, curr_y + 14), banner_text, fill=(255, 255, 255))
            curr_y += header_h
            canvas.paste(im, (0, curr_y))
            curr_y += im.size[1] + gap_h

        board_name = f"{prefix}_{b_idx:02d}.png"
        board_file = out_folder / board_name
        canvas.save(board_file)
        boards.append({
            "board_id": f"{prefix}_{b_idx:02d}",
            "path": str(board_file),
            "width": board_w,
            "height": total_h,
            "panel_count": len(rendered_panels),
            "panel_frame_ids": [m.get("frame_id", "") for _, _, m in rendered_panels],
            "panel_indices": [start_i + i + 1 for i in range(len(rendered_panels))],
            "panel_refs": [
                {"panel_index": start_i + i + 1, "frame_id": m.get("frame_id", ""),
                 "timestamp_sec": float(m.get("actual_sec", m.get("requested_sec", 0.0)))}
                for i, (_, _, m) in enumerate(rendered_panels)
            ],
            "timestamps_sec": [float(m.get("actual_sec", m.get("requested_sec", 0.0))) for _, _, m in rendered_panels],
            "sha256": file_sha256(board_file),
        })

    return boards


TUTORIAL_UI_CUE_TERMS = (
    "프롬프트", "예시", "커리어", "슬라이드", "복사", "개요", "붙여넣", "프레젠테이션",
    "생성", "응축", "요약", "보통", "상세", "카드", "테마", "사용자 지정", "이미지",
    "미디어", "업로드", "달리", "오픈AI", "클릭", "선택", "누르", "설정", "명령", "코드",
)


def select_curated_ui_timestamps(
    scenes: Sequence[SceneInterval | Dict[str, Any]],
    parsed_subtitles: Dict[str, Any],
    *,
    max_keyframes: int = 16,
    min_gap_sec: float = 4.5,
) -> List[float]:
    """Select temporally diverse, changing and cue-rich UI scenes.

    The temporal buckets are based on clock time (not scene-count quantiles).
    A second pixel-level deduplication pass runs after actual frame extraction.
    """
    if max_keyframes < 1:
        raise ValueError("max_keyframes must be positive")
    segments = parsed_subtitles.get("segments", []) or []
    candidates: List[Tuple[float, float]] = []
    for raw in scenes:
        sc = raw.to_dict() if isinstance(raw, SceneInterval) else dict(raw)
        sec = float(sc.get("keyframe_sec", sc.get("start_sec", 0.0)))
        start = float(sc.get("start_sec", sec))
        end = float(sc.get("end_sec", sec))
        density = max(0.0, float(sc.get("ui_density_score", 0.0)))
        novelty = max(0.0, float(sc.get("mad_score", 0.0)))
        cues = sum(
            sum(1 for term in TUTORIAL_UI_CUE_TERMS if term in str(seg.get("text", "")))
            for seg in segments
            if float(seg.get("start", 0)) < end + 1.0
            and float(seg.get("end", seg.get("start", 0))) >= start - 1.0
        )
        # A low-confidence UI classifier may not exclude a genuinely useful change.
        weight = 1.0 if sc.get("is_ui_dense", False) else 0.7
        score = weight * (density * 100.0 + min(60.0, novelty * 0.4) + 3.5 * cues)
        candidates.append((sec, score))
    if not candidates:
        return []
    candidates.sort(key=lambda item: item[0])
    # Remove exact timestamp duplicates before allocating temporal slots.
    scores = {}
    for sec, score in candidates:
        scores[sec] = max(score, scores.get(sec, float("-inf")))
    candidates = sorted(scores.items())
    if len(candidates) <= max_keyframes:
        return [round(sec, 3) for sec, _ in candidates]
    earliest, latest = candidates[0][0], candidates[-1][0]
    span = max(1.0, latest - earliest)
    buckets: List[List[Tuple[float, float]]] = [[] for _ in range(max_keyframes)]
    for sec, score in candidates:
        idx = min(max_keyframes - 1, int((sec - earliest) / span * max_keyframes))
        buckets[idx].append((sec, score))
    chosen: List[float] = []
    # First pass: cover video phases, reject near-colliding changes.
    for bucket in buckets:
        for sec, _ in sorted(bucket, key=lambda item: (-item[1], item[0])):
            if all(abs(sec - prev) >= min_gap_sec for prev in chosen):
                chosen.append(sec)
                break
    # Fill remaining capacity with high-value changes while enforcing spacing.
    if len(chosen) < max_keyframes:
        for sec, _ in sorted(candidates, key=lambda item: (-item[1], item[0])):
            if len(chosen) == max_keyframes:
                break
            if all(abs(sec - prev) >= min_gap_sec for prev in chosen):
                chosen.append(sec)
    return sorted(round(sec, 3) for sec in chosen)


def compute_adaptive_extraction_budget(
    media_metadata: Dict[str, Any],
    scenes: Sequence[SceneInterval | Dict[str, Any]],
    parsed_subtitles: Dict[str, Any],
) -> Dict[str, Any]:
    """Dynamically determine board count, UI state count, chapter count, and audio spot-check need per video.

    Avoids hardcoding 8 boards / 16 UI states / 12 chapters across all 77 videos by scaling
    extraction artifacts with video duration, UI scene density, and prompt/code cue frequency.
    """
    duration_sec = float(media_metadata.get("duration_sec") or 0.0)
    if duration_sec <= 0.0 and scenes:
        last_sc = scenes[-1].to_dict() if isinstance(scenes[-1], SceneInterval) else dict(scenes[-1])
        duration_sec = float(last_sc.get("end_sec", 60.0))
    duration_min = max(1.0, duration_sec / 60.0)

    scene_dicts = [s.to_dict() if isinstance(s, SceneInterval) else dict(s) for s in scenes]
    total_scenes = len(scene_dicts)
    ui_dense_scenes = [s for s in scene_dicts if s.get("is_ui_dense", False)]
    ui_dense_count = len(ui_dense_scenes)
    ui_dense_duration = sum(max(0.0, float(s.get("end_sec", 0.0)) - float(s.get("start_sec", 0.0))) for s in ui_dense_scenes)
    ui_dense_ratio = min(1.0, ui_dense_duration / max(1.0, duration_sec))
    avg_ui_density = (
        sum(float(s.get("ui_density_score", 0.0)) for s in ui_dense_scenes) / max(1, ui_dense_count)
        if ui_dense_count > 0
        else 0.0
    )

    segments = parsed_subtitles.get("segments", []) or []
    cue_segments = 0
    max_subtitle_gap_sec = 0.0
    prev_end = 0.0
    for seg in segments:
        txt = str(seg.get("text", ""))
        if any(term in txt for term in TUTORIAL_UI_CUE_TERMS):
            cue_segments += 1
        s_start = float(seg.get("start", 0.0))
        s_end = float(seg.get("end", s_start))
        if s_start - prev_end > max_subtitle_gap_sec:
            max_subtitle_gap_sec = s_start - prev_end
        prev_end = max(prev_end, s_end)

    cue_per_min = cue_segments / duration_min
    transitions_per_min = ui_dense_count / duration_min

    # Dynamic scaling formulas (bounded to prevent under-sampling or single-turn context overflow)
    target_chapters = max(4, min(36, int(round(duration_min * 1.35 + ui_dense_count * 0.08))))
    target_ui_states = max(
        6,
        min(
            48,
            int(round(duration_min * 1.6 + ui_dense_count * 0.18 + cue_segments * 0.14)),
        ),
    )
    # Round up to even number for 2-panel boards
    if target_ui_states % 2 == 1:
        target_ui_states += 1
    panels_per_board = 2
    target_boards = target_ui_states // panels_per_board
    # Keep at most 10 boards per single-turn AGY invocation to prevent context bloat
    max_boards_per_session = 10
    session_batches = max(1, (target_boards + max_boards_per_session - 1) // max_boards_per_session)

    needs_audio_spot_check = (len(segments) / duration_min < 6.0) or (max_subtitle_gap_sec >= 20.0)

    if ui_dense_ratio < 0.25 and cue_per_min < 2.0:
        complexity_tier = "LOW_TALKING_HEAD"
    elif avg_ui_density >= 0.085 or cue_per_min >= 6.0 or duration_min >= 20.0:
        complexity_tier = "HIGH_DENSE_CODE_OR_FINE_UI"
    else:
        complexity_tier = "MEDIUM_STANDARD_TUTORIAL"

    return {
        "duration_sec": round(duration_sec, 3),
        "duration_min": round(duration_min, 3),
        "total_scenes": total_scenes,
        "ui_dense_scene_count": ui_dense_count,
        "ui_dense_ratio": round(ui_dense_ratio, 4),
        "avg_ui_density_score": round(avg_ui_density, 4),
        "subtitle_segment_count": len(segments),
        "cue_segment_count": cue_segments,
        "cue_per_min": round(cue_per_min, 3),
        "ui_transitions_per_min": round(transitions_per_min, 3),
        "max_subtitle_gap_sec": round(max_subtitle_gap_sec, 3),
        "target_chapters": target_chapters,
        "target_ui_states": target_ui_states,
        "panels_per_board": panels_per_board,
        "target_boards": target_boards,
        "session_batches": session_batches,
        "needs_audio_spot_check": needs_audio_spot_check,
        "complexity_tier": complexity_tier,
    }


PRIMARY_MODEL_CHOICES = ("argon", "flash", "auto")
PRIMARY_MODEL_ALIASES: Dict[str, Optional[str]] = {
    "argon": "gemini-4-argon-high",
    "gemini-4-argon-high": "gemini-4-argon-high",
    "gemini-4-argon": "gemini-4-argon-high",
    "flash": "gemini-3.8-flash-medium",
    "gemini-3.8-flash-medium": "gemini-3.8-flash-medium",
    "gemini-3.8-flash": "gemini-3.8-flash-medium",
    "auto": None,
}


def resolve_primary_model_choice(prefer_model: Optional[str]) -> Tuple[Optional[str], str]:
    """Normalize CLI/API primary model choice ('argon', 'flash', 'auto', or explicit model ID)."""
    if prefer_model is None:
        return None, "auto"
    key = str(prefer_model).strip().lower()
    if key not in PRIMARY_MODEL_ALIASES:
        raise ValueError(
            f"INVALID_PRIMARY_MODEL: {prefer_model!r} (expected one of {PRIMARY_MODEL_CHOICES})"
        )
    resolved = PRIMARY_MODEL_ALIASES[key]
    if resolved is None:
        return None, "auto"
    mode = "argon" if "argon" in resolved else "flash"
    return resolved, mode


def select_adaptive_model_policy(
    budget: Dict[str, Any],
    *,
    prefer_model: Optional[str] = None,
    flash_tokens_per_board: float = 5500.0,
    argon_tokens_per_board: float = 4800.0,
) -> Dict[str, Any]:
    """Select the optimal model execution policy per stage without hardcoding a single model assumption."""
    tier = str(budget.get("complexity_tier", "MEDIUM_STANDARD_TUTORIAL"))
    boards = int(budget.get("target_boards", 8))
    explicit_model, mode = resolve_primary_model_choice(prefer_model)

    if explicit_model in ("gemini-3.8-flash-medium", "gemini-4-argon-high"):
        primary_model = explicit_model
    elif tier == "HIGH_DENSE_CODE_OR_FINE_UI":
        # High UI/code density favors Argon's zero-retry precision and lower thinking-token overhead
        primary_model = "gemini-4-argon-high"
    elif tier == "LOW_TALKING_HEAD":
        primary_model = "gemini-3.8-flash-medium"
    else:
        # Choose whichever model has lower expected total token cost per board while meeting 100% quality
        primary_model = (
            "gemini-4-argon-high"
            if argon_tokens_per_board <= flash_tokens_per_board
            else "gemini-3.8-flash-medium"
        )

    return {
        "complexity_tier": tier,
        "python_only_stages": [
            "media_cached",
            "subtitles_parsed",
            "metadata_probed",
            "scenes_detected",
            "frames_extracted",
            "timeline_aligned",
            "verification_saved",
            "markdown_assembled",
        ],
        "execution_architecture": "PYTHON_DIRECT_CHILD_CLI",
        "invoke_interactive_main_argon_per_video": False,
        "primary_model_mode": mode,
        "primary_model": primary_model,
        "primary_effort": "high" if "argon" in primary_model else "medium",
        "independence_mode": "INDEPENDENT_ARGON" if "argon" in primary_model else "INDEPENDENT_FLASH",
        "selective_escalation_model": "gemini-4-argon-high",
        "enable_selective_escalation": (primary_model != "gemini-4-argon-high") or (boards > 10),
        "skip_unproductive_reanalysis": True,
        "needs_audio_spot_check": bool(budget.get("needs_audio_spot_check", False)),
        "verify_child_init_model": True,
        "agy_quota_deduction": "UNKNOWN",
    }


# ---------------------------------------------------------------------------
# 6. Video-Subtitle Time-Axis Alignment
# ---------------------------------------------------------------------------

def align_multimodal_timeline(
    scenes: Sequence[SceneInterval | Dict[str, Any]],
    parsed_subtitles: Dict[str, Any],
    *,
    extracted_frames: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Align video scene intervals and extracted keyframes with JSON3 subtitle segments."""
    segments = parsed_subtitles.get("segments", [])
    frames_list = list(extracted_frames or [])
    aligned_units: List[Dict[str, Any]] = []

    for raw_scene in scenes:
        sc = raw_scene.to_dict() if isinstance(raw_scene, SceneInterval) else dict(raw_scene)
        s_start = float(sc["start_sec"])
        s_end = float(sc["end_sec"])

        # Match subtitle segments whose midpoint or start falls within [s_start, s_end]
        matched_subs: List[Dict[str, Any]] = []
        for seg in segments:
            seg_start = float(seg["start"])
            seg_end = float(seg.get("end", seg_start + float(seg.get("duration", 0.0))))
            midpoint = (seg_start + seg_end) / 2.0
            if (s_start <= seg_start < s_end) or (s_start <= midpoint < s_end):
                matched_subs.append({
                    "segment_id": seg["id"],
                    "t_start_ms": seg["t_start_ms"],
                    "d_duration_ms": seg["d_duration_ms"],
                    "start": seg_start,
                    "end": seg_end,
                    "timestamp": seg["timestamp"],
                    "text": seg["text"],
                })

        matched_frames = [
            f for f in frames_list
            if (s_start - 0.5) <= float(f.get("actual_sec", f.get("requested_sec", -1.0))) <= (s_end + 0.5)
        ]

        aligned_units.append({
            "scene_id": sc["scene_id"],
            "start_sec": s_start,
            "end_sec": s_end,
            "start_mmss": format_mmss(s_start),
            "end_mmss": format_mmss(s_end),
            "keyframe_sec": sc.get("keyframe_sec", s_start),
            "mad_score": sc.get("mad_score", 0.0),
            "ui_density_score": sc.get("ui_density_score", 0.0),
            "is_ui_dense": bool(sc.get("is_ui_dense", False)),
            "keyframes": matched_frames,
            "segment_ids": [m["segment_id"] for m in matched_subs],
            "subtitles": matched_subs,
            "combined_transcript_text": " ".join(m["text"] for m in matched_subs),
        })

    return {
        "schema_version": 1,
        "video_id": parsed_subtitles.get("video_id", ""),
        "total_scenes": len(aligned_units),
        "total_subtitles": len(segments),
        "ui_dense_scene_count": sum(1 for u in aligned_units if u["is_ui_dense"]),
        "units": aligned_units,
    }


# ---------------------------------------------------------------------------
# 7, 8, 9. Flash Default Analysis + Selective Re-analysis + Conditional Argon
# ---------------------------------------------------------------------------

UNCERTAINTY_KEYWORDS = (
    "UNVERIFIED",
    "UNCERTAIN",
    "CONFLICT",
    "추정",
    "추론",
    "불분명",
    "해상도 한계",
    "식별 불가",
)


def detect_uncertain_intervals(
    aligned_timeline: Dict[str, Any],
    flash_analysis: Dict[str, Any],
    *,
    confidence_threshold: float = 0.85,
    high_ui_density_threshold: float = 0.06,
) -> List[Dict[str, Any]]:
    """Identify only the uncertain, conflicting, or high-UI-density intervals needing re-analysis."""
    flagged: Dict[int, Dict[str, Any]] = {}

    for item in flash_analysis.get("scene_analyses", []) or []:
        scene_id = int(item.get("scene_id", 0))
        unit = resolve_chapter_source_units(aligned_timeline, item)
        reasons: List[str] = []

        confidence = float(item.get("confidence", 1.0))
        status = str(item.get("verification_status", "VERIFIED")).upper()
        text_blob = json.dumps(item, ensure_ascii=False)

        if confidence < confidence_threshold:
            reasons.append(f"low_confidence:{confidence:.2f}")
        if status in ("UNVERIFIED", "UNCERTAIN", "CONFLICT", "PARTIAL"):
            reasons.append(f"status:{status}")
        for kw in UNCERTAINTY_KEYWORDS:
            if kw in text_blob:
                reasons.append(f"keyword:{kw}")
                break

        # Detect audio vs visual version-number over-inference (e.g., claiming "DALL-E 3" when transcript only says "달리")
        transcript_text = str(unit.get("combined_transcript_text", ""))
        visual_claims_text = str(item.get("visual_ui_text", "")) + " " + str(item.get("summary", ""))
        if ("DALL-E 3" in visual_claims_text or "DALL·E 3" in visual_claims_text) and "3" not in transcript_text:
            reasons.append("audio_visual_version_conflict:DALL-E_3_vs_spoken_dalle")

        # High UI density scenes with prompts or small UI controls that lacked high-res crops
        ui_density = float(unit.get("ui_density_score", 0.0))
        evidence_ref = str(item.get("evidence_ref", ""))
        has_highres = (
            any(int(f.get("source_height", 0) or 0) >= 1080 for f in unit.get("keyframes", []))
            or ("board_" in evidence_ref)
        )
        if ui_density >= high_ui_density_threshold and not has_highres:
            reasons.append(f"high_ui_density_without_highres:{ui_density:.4f}")

        if reasons:
            flagged[scene_id] = {
                "scene_id": scene_id,
                "start_sec": unit.get("start_sec", item.get("start_sec", 0.0)),
                "end_sec": unit.get("end_sec", item.get("end_sec", 0.0)),
                "keyframe_sec": unit.get("keyframe_sec", item.get("start_sec", 0.0)),
                "reasons": reasons,
                "original_confidence": confidence,
                "original_status": status,
            }

    return [flagged[k] for k in sorted(flagged.keys())]


def should_escalate_to_argon(
    uncertain_intervals: Sequence[Dict[str, Any]],
    *,
    max_flash_retries_completed: bool = True,
    conflict_escalation_threshold: int = 1,
) -> Tuple[bool, List[str]]:
    """Determine whether Gemini 4 Argon should be conditionally invoked on remaining uncertain intervals."""
    if not uncertain_intervals:
        return False, []
    reasons: List[str] = []
    conflict_count = 0
    for interval in uncertain_intervals:
        for r in interval.get("reasons", []):
            if "conflict" in r.lower() or "status:CONFLICT" in r or "status:UNVERIFIED" in r or "low_confidence" in r:
                conflict_count += 1
                reasons.append(f"scene_{interval['scene_id']}:{r}")
    if max_flash_retries_completed and (conflict_count >= conflict_escalation_threshold or len(uncertain_intervals) > 0):
        return True, reasons or [f"unresolved_intervals:{len(uncertain_intervals)}"]
    return False, []


def run_tiered_multimodal_analysis(
    aligned_timeline: Dict[str, Any],
    *,
    flash_analyzer: Callable[[Dict[str, Any]], Dict[str, Any]],
    selective_highres_reanalyzer: Optional[Callable[[List[Dict[str, Any]], Dict[str, Any]], Dict[str, Any]]] = None,
    argon_verifier: Optional[Callable[[List[Dict[str, Any]], Dict[str, Any]], Dict[str, Any]]] = None,
    confidence_threshold: float = 0.85,
    primary_model: str = "gemini-3.8-flash-medium",
) -> Dict[str, Any]:
    """Execute primary model analysis -> selective high-res re-analysis -> conditional Argon verification."""
    flash_result = flash_analyzer(aligned_timeline)
    initial_uncertain = detect_uncertain_intervals(
        aligned_timeline,
        flash_result,
        confidence_threshold=confidence_threshold,
    )

    reanalyzed_scenes: Dict[int, Dict[str, Any]] = {}
    remaining_uncertain = list(initial_uncertain)

    if initial_uncertain and selective_highres_reanalyzer is not None:
        re_output = selective_highres_reanalyzer(initial_uncertain, aligned_timeline)
        for updated in re_output.get("scene_analyses", []) or []:
            sid = int(updated["scene_id"])
            reanalyzed_scenes[sid] = updated

        # Merge selective re-analysis back into flash_result
        merged_scenes = []
        for orig in flash_result.get("scene_analyses", []) or []:
            sid = int(orig["scene_id"])
            merged_scenes.append(reanalyzed_scenes.get(sid, orig))
        flash_result = dict(flash_result, scene_analyses=merged_scenes)
        remaining_uncertain = detect_uncertain_intervals(
            aligned_timeline,
            flash_result,
            confidence_threshold=confidence_threshold,
        )

    escalate, escalation_reasons = should_escalate_to_argon(remaining_uncertain)
    argon_invoked = False
    argon_verified_scenes: Dict[int, Dict[str, Any]] = {}

    if escalate and argon_verifier is not None:
        argon_invoked = True
        argon_output = argon_verifier(remaining_uncertain, aligned_timeline)
        for verified in argon_output.get("scene_analyses", []) or []:
            sid = int(verified["scene_id"])
            argon_verified_scenes[sid] = verified

        final_scenes = []
        for sc in flash_result.get("scene_analyses", []) or []:
            sid = int(sc["scene_id"])
            final_scenes.append(argon_verified_scenes.get(sid, sc))
        flash_result = dict(flash_result, scene_analyses=final_scenes)

    return {
        "schema_version": 1,
        "video_id": aligned_timeline.get("video_id", ""),
        "default_model": primary_model,
        "primary_model": primary_model,
        "verifier_model": "gemini-4-argon-high" if argon_invoked else None,
        "initial_uncertain_count": len(initial_uncertain),
        "initial_uncertain_intervals": initial_uncertain,
        "selectively_reanalyzed_scene_ids": sorted(reanalyzed_scenes.keys()),
        "remaining_uncertain_count": len(remaining_uncertain),
        "remaining_uncertain_intervals": remaining_uncertain,
        "argon_invoked": argon_invoked,
        "argon_escalation_reasons": escalation_reasons,
        "argon_verified_scene_ids": sorted(argon_verified_scenes.keys()),
        "scene_analyses": flash_result.get("scene_analyses", []),
        "metadata_summary": flash_result.get("metadata_summary", {}),
        "prompts_extracted": flash_result.get("prompts_extracted", []),
        "ui_settings_extracted": flash_result.get("ui_settings_extracted", []),
        "research_items": flash_result.get("research_items", []),
        "workflow_steps": flash_result.get("workflow_steps", []),
        "cli_commands": flash_result.get("cli_commands", []),
    }


# ---------------------------------------------------------------------------
# 10. Structured Storage of Verification Results
# ---------------------------------------------------------------------------

def verification_path(root: Path, video_id: str) -> Path:
    return video_dir(root, video_id) / "verification.json"


def save_verification_results(
    root: Path,
    video_id: str,
    items: Sequence[Dict[str, Any]],
    *,
    summary_stats: Optional[Dict[str, Any]] = None,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist structured evidence verification items with provenance."""
    counts = {
        "VERIFIED": 0, "SELF_CHECKED": 0,
        "MODEL_CROSS_CHECKED": 0, "INDEPENDENT_VERIFIED": 0,
        "PARTIAL": 0, "CONFLICT": 0, "UNVERIFIED": 0,
    }
    normalized_items = []
    for idx, item in enumerate(items, start=1):
        status = str(item.get("final_status", item.get("status", "UNVERIFIED"))).upper()
        if status not in counts:
            counts[status] = 0
        counts[status] += 1
        normalized_items.append({
            "item_id": item.get("item_id", idx),
            "category": item.get("category", "general"),
            "timestamp": item.get("timestamp", "00:00"),
            "claim": item.get("claim", ""),
            "flash_verdict": item.get("flash_verdict", "UNKNOWN"),
            "argon_verdict": item.get("argon_verdict", "UNKNOWN"),
            "final_status": status,
            "evidence_type": item.get("evidence_type", "VISUAL_FRAME"),
            "evidence_location": item.get("evidence_location", ""),
            "ground_truth": item.get("ground_truth"),
            "primary_model": item.get("primary_model"),
            "primary_model_verdict": item.get("primary_model_verdict"),
            "chapter_id": item.get("chapter_id"),
            "source_scene_ids": item.get("source_scene_ids", []),
            "verification_method": item.get("verification_method", "UNSPECIFIED"),
            "independent_verifier_invoked": bool(item.get("independent_verifier_invoked", False)),
            "human_review_required": bool(item.get("human_review_required", True)),
        })

    payload = {
        "schema_version": 1,
        "video_id": video_id,
        "updated_utc": now_iso(),
        "total_items": len(normalized_items),
        "status_counts": counts,
        "summary_stats": summary_stats or {},
        "items": normalized_items,
    }
    target = Path(output_path) if output_path is not None else verification_path(root, video_id)
    atomic_write(target, json_text(payload))
    return payload


def load_verification_results(root: Path, video_id: str) -> Optional[Dict[str, Any]]:
    path = verification_path(root, video_id)
    if not path.exists():
        return None
    return read_json(path)


# ---------------------------------------------------------------------------
# 11. Code-Based Deterministic Final Markdown Assembly
# ---------------------------------------------------------------------------

def assemble_final_markdown(
    *,
    video_id: str,
    title: str,
    url: str,
    media_metadata: Dict[str, Any],
    analysis_result: Dict[str, Any],
    parsed_subtitles: Dict[str, Any],
    verification_report: Optional[Dict[str, Any]] = None,
) -> str:
    """Assemble the complete final Markdown report in Python code without spending LLM output tokens."""
    lines: List[str] = []
    lines.append(f"# {title} — 근거 출처 표시 멀티모달 분석 보고서 (`{video_id}`)")
    lines.append("")
    lines.append("## 1. 영상 및 미디어 메타데이터 (코드 자동 추출)")
    lines.append("")
    lines.append(f"- **영상 ID**: `{video_id}`")
    lines.append(f"- **원본 URL**: {url}")

    duration_sec = media_metadata.get("duration_sec")
    if duration_sec is not None:
        lines.append(f"- **재생 시간**: `{format_mmss(duration_sec)}` (`{duration_sec:.3f}s`)")

    for stream in media_metadata.get("streams", []) or []:
        if stream.get("type") == "video":
            lines.append(
                f"- **비디오 스트림**: 코덱 `{stream.get('codec_name')}` (`{stream.get('codec_tag')}`), "
                f"해상도 `{stream.get('width')}x{stream.get('height')}`, `{stream.get('fps')} fps`"
            )
        elif stream.get("type") == "audio":
            lines.append(
                f"- **오디오 스트림**: 코덱 `{stream.get('codec_name')}` (`{stream.get('codec_tag')}`), "
                f"`{stream.get('sample_rate')} Hz`, `{stream.get('channels')} ch`"
            )

    tr = parsed_subtitles.get("time_range_ms", {})
    lines.append(
        f"- **자막 커버리지 (JSON3 코드 파싱)**: 총 `{parsed_subtitles.get('raw_event_count', 0)}`개 이벤트 중 "
        f"유효 세그먼트 `{parsed_subtitles.get('valid_segment_count', 0)}`개 "
        f"(`{tr.get('first_start_ms', 0)}ms ~ {tr.get('last_end_ms', 0)}ms`)"
    )
    lines.append(
        f"- **분석 파이프라인**: 기본 모델 `{analysis_result.get('default_model', 'gemini-3.8-flash-medium')}` / "
        f"조건부 상위 검증 `{analysis_result.get('verifier_model') or '미호출 (조건 미충족)'}`"
    )
    lines.append("")

    # Section 2: Timeline chapters (NOT raw detected scene IDs)
    lines.append("## 2. 타임라인별 화면·음성 통합 분석 (논리 챕터)")
    lines.append("")
    ver_by_chapter = {int(x.get("chapter_id") or x.get("item_id", -1)): x
                      for x in (verification_report or {}).get("items", [])}
    for sc in analysis_result.get("scene_analyses", []) or []:
        start_mmss = format_mmss(float(sc.get("start_sec", 0.0)))
        end_mmss = format_mmss(float(sc.get("end_sec", 0.0)))
        chapter_id = sc.get("chapter_id", sc.get("scene_id"))
        status_badge = ver_by_chapter.get(int(chapter_id), {}).get("final_status", "UNVERIFIED")
        model_tag = sc.get("verified_by", analysis_result.get("default_model", "flash"))
        source_ids = ", ".join(map(str, sc.get("source_scene_ids", []))) or "미지정"
        lines.append(f"### [{start_mmss} ~ {end_mmss}] Chapter {chapter_id} (`{status_badge}` | `{model_tag}`)")
        lines.append(f"- **원본 탐지 장면 ID**: {source_ids} (챕터 ID와 별개)")
        if sc.get("summary"):
            lines.append(f"- **핵심 요약**: {sc['summary']}")
        if sc.get("visual_ui_text"):
            lines.append(f"- **화면(UI) 판독 내용**: {sc['visual_ui_text']}")
        if sc.get("spoken_summary"):
            lines.append(f"- **음성(발화) 내용**: {sc['spoken_summary']}")
        lines.append("")

    # Section 3: Extracted Prompts
    prompts = analysis_result.get("prompts_extracted", []) or []
    if prompts:
        lines.append("## 3. 화면 텍스트·사용자 프롬프트 발췌 (원문 완전성 검증 대기)")
        lines.append("")
        for p in prompts:
            lines.append(f"### {p.get('label', '화면 텍스트')} (`{p.get('timestamp', '00:00')}`)")
            lines.append(f"- **증거 유형**: `{p.get('evidence_kind', 'SCREEN_TEXT_UNSPECIFIED')}`")
            lines.append(f"- **전사 완전성**: `{p.get('transcription_completeness', 'UNCONFIRMED_COMPLETE')}` (별도 시각 검수 전)")
            lines.append(f"- **근거**: `{p.get('evidence_ref', '')}`")
            lines.append("```text")
            lines.append(str(p.get("verbatim_text", "")))
            lines.append("```")
            lines.append("")

    # Section 4: UI Settings Table
    ui_settings = analysis_result.get("ui_settings_extracted", []) or []
    if ui_settings:
        lines.append("## 4. 화면 UI 설정값 검증 표")
        lines.append("")
        lines.append("| 시각 | UI 패널 / 항목 | 실제 선택·표시값 | 검증 상태 | 근거 프레임 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for u in ui_settings:
            lines.append(
                f"| `{u.get('timestamp', '')}` | {u.get('setting_name', '')} | "
                f"`{u.get('selected_value', '')}` | `{u.get('status', 'VERIFIED')}` | `{u.get('evidence_ref', '')}` |"
            )
        lines.append("")

    # Section 5: Verification Summary if provided
    if verification_report:
        counts = verification_report.get("status_counts", {})
        lines.append("## 5. 구조화된 증거 감사 요약")
        lines.append("")
        lines.append(f"- 총 평가 항목: `{verification_report.get('total_items', 0)}`건")
        lines.append("- **주의**: 1차 모델 자체 판정은 독립 검증이나 정답 데이터가 아닙니다.")
        for status in ("SELF_CHECKED", "MODEL_CROSS_CHECKED", "INDEPENDENT_VERIFIED", "PARTIAL", "CONFLICT", "UNVERIFIED"):
            lines.append(f"- `{status}`: `{counts.get(status, 0)}`건")
        lines.append("")

    # Section 6: Full Code-Assembled Subtitle Table (0 LLM output tokens!)
    lines.append("## 6. 전체 타임라인 자막 세그먼트 원문 (JSON3 코드 직접 조립)")
    lines.append("")
    lines.append("| 세그먼트 ID | 시작 시각 | 시작(ms) | 지속(ms) | 종료(ms) | 발화 원문 |")
    lines.append("| :--- | :--- | ---: | ---: | ---: | :--- |")
    for seg in parsed_subtitles.get("segments", []) or []:
        safe_text = str(seg.get("text", "")).replace("|", "\\|")
        lines.append(
            f"| `{seg['id']}` | `{seg['timestamp']}` | `{seg['t_start_ms']}` | "
            f"`{seg['d_duration_ms']}` | `{seg['t_end_ms']}` | {safe_text} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 12. Checkpoint-Based Crash Recovery & Resumption
# ---------------------------------------------------------------------------

MULTIMODAL_STAGES = (
    "media_cached",
    "subtitles_parsed",
    "metadata_probed",
    "scenes_detected",
    "frames_extracted",
    "timeline_aligned",
    "tiered_analyzed",
    "verification_saved",
    "markdown_assembled",
)

MULTIMODAL_PROMPT_VERSION = "v3_5_claim_provenance_workflow_quality"
DEFAULT_CROP_BOX: Tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)


def compute_multimodal_run_id(
    *,
    primary_model: str,
    primary_model_mode: str = "auto",
    use_agy_argon_verifier: bool = False,
    prompt_signature: Optional[str] = None,
    analyzer_signature: str = "agy_cli",
    panels_per_board: int = 2,
    max_keyframes: int = 16,
    crop_box: Optional[Tuple[float, float, float, float]] = DEFAULT_CROP_BOX,
    board_paths: Optional[Sequence[Path | str]] = None,
    roi_mode: str = "auto",
    run_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Compute a deterministic run ID and configuration manifest isolating model, prompt, and Evidence Board setups."""
    effective_prompt_sig = prompt_signature or MULTIMODAL_PROMPT_VERSION
    normalized_boards = [Path(str(p)).name for p in board_paths] if board_paths else []
    board_config = {
        "panels_per_board": int(panels_per_board),
        "max_keyframes": int(max_keyframes),
        "crop_box": [round(float(x), 4) for x in crop_box] if crop_box else None,
        "roi_mode": roi_mode,
        "roi_boxes": {k: list(v) for k, v in ROI_BOXES.items()} if roi_mode == "auto" else {},
        "custom_boards": normalized_boards,
    }
    board_config_hash = digest(json_text(board_config))[:8]
    run_config: Dict[str, Any] = {
        "primary_model": primary_model,
        "primary_model_mode": str(primary_model_mode).lower(),
        "independence_mode": classify_run_independence(primary_model, prompt_text=effective_prompt_sig),
        "use_agy_argon_verifier": bool(use_agy_argon_verifier),
        "prompt_signature": effective_prompt_sig,
        "analyzer_signature": analyzer_signature,
        "evidence_board_config": board_config,
        "board_config_hash": board_config_hash,
    }
    config_hash = digest(json_text(run_config))[:8]
    run_config["config_hash"] = config_hash

    if run_id:
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(run_id).strip()).strip("_")
        resolved_id = safe_id or f"run_{config_hash}"
    else:
        model_lower = primary_model.lower()
        model_short = "argon" if "argon" in model_lower else ("flash" if "flash" in model_lower else "model")
        resolved_id = f"{model_short}_b{int(panels_per_board)}x{int(max_keyframes)}_{config_hash}"

    return resolved_id, run_config


class MultimodalCheckpointManager:
    """Manages stage-level checkpoints so interrupted video runs resume without repeating work."""

    def __init__(
        self,
        root: Path,
        video_id: str,
        *,
        run_id: Optional[str] = None,
        run_config: Optional[Dict[str, Any]] = None,
    ):
        self.root = Path(root).resolve()
        self.video_id = video_id
        self.run_id = run_id
        self.run_config = dict(run_config) if run_config else None
        vdir = video_dir(self.root, self.video_id)
        if run_id:
            self.run_dir = vdir / "runs" / run_id
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.path = self.run_dir / "multimodal_checkpoint.json"
        else:
            self.run_dir = vdir
            self.path = vdir / "multimodal_checkpoint.json"

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            payload: Dict[str, Any] = {
                "schema_version": 1,
                "video_id": self.video_id,
                "status": "PENDING",
                "last_completed_stage": None,
                "stages": {},
                "updated_utc": now_iso(),
            }
            if self.run_id is not None:
                payload["run_id"] = self.run_id
            if self.run_config is not None:
                payload["run_config"] = self.run_config
            return payload
        return read_json(self.path)

    def is_stage_done(self, stage_name: str) -> bool:
        state = self.load()
        stage_info = state.get("stages", {}).get(stage_name)
        if not stage_info or stage_info.get("status") != "DONE":
            return False
        artifact = stage_info.get("artifact_path")
        if artifact and not Path(artifact).exists():
            return False
        return True

    def mark_stage_done(
        self,
        stage_name: str,
        *,
        artifact_path: Optional[Path] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        state = self.load()
        if self.run_id is not None:
            state["run_id"] = self.run_id
        if self.run_config is not None:
            state["run_config"] = self.run_config
        stages = dict(state.get("stages", {}))
        stages[stage_name] = {
            "status": "DONE",
            "completed_utc": now_iso(),
            "artifact_path": str(artifact_path) if artifact_path else None,
            "metadata": metadata or {},
        }
        state["stages"] = stages
        state["last_completed_stage"] = stage_name
        state["status"] = "COMPLETED" if all(s in stages and stages[s]["status"] == "DONE" for s in MULTIMODAL_STAGES) else "IN_PROGRESS"
        state["updated_utc"] = now_iso()
        atomic_write(self.path, json_text(state))
        return state

    def mark_failed(self, stage_name: str, error_message: str) -> Dict[str, Any]:
        state = self.load()
        if self.run_id is not None:
            state["run_id"] = self.run_id
        if self.run_config is not None:
            state["run_config"] = self.run_config
        stages = dict(state.get("stages", {}))
        stages[stage_name] = {
            "status": "FAILED",
            "failed_utc": now_iso(),
            "error": str(error_message),
        }
        state["stages"] = stages
        state["status"] = "FAILED"
        state["failed_stage"] = stage_name
        state["updated_utc"] = now_iso()
        atomic_write(self.path, json_text(state))
        return state


def _can_sync_top_level_artifacts(vdir: Path, resolved_run_id: str) -> bool:
    """Return True only if writing top-level artifacts will not overwrite another run or legacy C2/C3/E outputs."""
    legacy_markers = (
        vdir / "tiered_analysis_argon_e.json",
        vdir / "tiered_analysis_flash_blind_c3.json",
        vdir / "tiered_analysis_pass1_c1.json",
        vdir / "argon_selective_verification.json",
    )
    if any(p.exists() for p in legacy_markers):
        return False
    top_ckpt = vdir / "multimodal_checkpoint.json"
    if top_ckpt.exists():
        try:
            existing_ckpt = read_json(top_ckpt)
            if existing_ckpt.get("run_id") != resolved_run_id:
                return False
        except Exception:
            return False
    top_analysis = vdir / "tiered_analysis.json"
    if top_analysis.exists():
        try:
            existing_analysis = read_json(top_analysis)
            if existing_analysis.get("run_id") != resolved_run_id:
                return False
        except Exception:
            return False
    return True


def run_resumable_multimodal_pipeline(
    root: Path,
    video_id: str,
    *,
    video_path: Path,
    subtitle_json3_path: Path,
    audio_path: Optional[Path] = None,
    highres_video_path: Optional[Path] = None,
    title: Optional[str] = None,
    url: Optional[str] = None,
    primary_model: str = "auto",
    flash_analyzer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    selective_highres_reanalyzer: Optional[Callable[[List[Dict[str, Any]], Dict[str, Any]], Dict[str, Any]]] = None,
    argon_verifier: Optional[Callable[[List[Dict[str, Any]], Dict[str, Any]], Dict[str, Any]]] = None,
    use_agy_flash: bool = False,
    use_agy_argon: bool = False,
    board_paths: Optional[Sequence[Path | str]] = None,
    panels_per_board: int = 2,
    max_keyframes: int = 16,
    crop_box: Optional[Tuple[float, float, float, float]] = DEFAULT_CROP_BOX,
    roi_mode: str = "auto",
    prompt_signature: Optional[str] = None,
    run_id: Optional[str] = None,
    copy_files: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Run the end-to-end multimodal pipeline with stage-by-stage checkpointing, media caching, and run isolation."""
    # Validate options before touching cached media.
    if roi_mode not in ("auto", "off"):
        raise ValueError("roi_mode must be auto or off")
    explicit_model_id, normalized_mode = resolve_primary_model_choice(primary_model)

    vdir = video_dir(root, video_id)
    vdir.mkdir(parents=True, exist_ok=True)
    has_protected_legacy = any(
        (vdir / name).exists()
        for name in (
            "tiered_analysis_argon_e.json",
            "tiered_analysis_flash_blind_c3.json",
            "multimodal_checkpoint.json",
        )
    )

    # Stage 1: Raw media caching into data/videos/{video_id}/media/ (preserving existing cache)
    cache_file = media_cache_manifest_path(root, video_id)
    manifest = cache_raw_media_bundle(
        root,
        video_id,
        video_path=video_path,
        audio_path=audio_path,
        subtitle_path=subtitle_json3_path,
        highres_video_path=highres_video_path,
        copy_files=copy_files,
    )

    assets = manifest.get("assets", {})
    cached_video_path = Path(assets.get("video", {}).get("cached_path") or video_path).resolve()
    cached_subtitle_path = Path(assets.get("subtitle_json3", {}).get("cached_path") or subtitle_json3_path).resolve()
    cached_audio_path = (
        Path(assets["audio"]["cached_path"]).resolve()
        if ("audio" in assets and assets["audio"].get("cached_path"))
        else (Path(audio_path).resolve() if audio_path else None)
    )
    cached_highres_path = (
        Path(assets["highres_video"]["cached_path"]).resolve()
        if ("highres_video" in assets and assets["highres_video"].get("cached_path"))
        else (Path(highres_video_path).resolve() if highres_video_path else None)
    )

    # Stage 2: Code-based JSON3 subtitle parsing
    parsed_sub_path = vdir / "parsed_subtitles.json"
    if not parsed_sub_path.exists() or (force and not has_protected_legacy):
        parsed_subtitles = parse_json3_subtitles(cached_subtitle_path, video_id=video_id)
        atomic_write(parsed_sub_path, json_text(parsed_subtitles))
    else:
        parsed_subtitles = read_json(parsed_sub_path)

    # Stage 3: Media metadata probing
    meta_path = vdir / "media_metadata.json"
    if not meta_path.exists() or (force and not has_protected_legacy):
        primary_vid = cached_highres_path if (cached_highres_path and cached_highres_path.exists()) else cached_video_path
        media_metadata = probe_media_metadata(primary_vid)
        if cached_audio_path and cached_audio_path.exists():
            audio_meta = probe_media_metadata(cached_audio_path)
            media_metadata["audio_companion"] = audio_meta
            for s in audio_meta.get("streams", []):
                if s.get("type") == "audio":
                    media_metadata["streams"].append(s)
        atomic_write(meta_path, json_text(media_metadata))
    else:
        media_metadata = read_json(meta_path)

    # Stage 4: Scene transition detection
    # Detector V3 is isolated: preserve prior scenes.json and old run evidence.
    scenes_path = vdir / "scenes_full_v3.json"
    if not scenes_path.exists() or (force and not has_protected_legacy):
        scenes_objs = detect_scene_transitions(cached_video_path, roi_box=DEFAULT_CROP_BOX)
        scenes_payload = {
            "video_id": video_id,
            "scene_count": len(scenes_objs),
            "scenes": [s.to_dict() for s in scenes_objs],
        }
        atomic_write(scenes_path, json_text(scenes_payload))
    else:
        scenes_payload = read_json(scenes_path)

    # Compute adaptive extraction budget and resolve primary model policy
    budget = compute_adaptive_extraction_budget(
        media_metadata,
        scenes_payload.get("scenes", []),
        parsed_subtitles,
    )
    policy = select_adaptive_model_policy(budget, prefer_model=primary_model)
    resolved_primary_model = str(policy["primary_model"])
    primary_effort = str(policy["primary_effort"])

    use_agy_for_primary = bool(
        flash_analyzer is None
        and (explicit_model_id is not None or use_agy_flash or use_agy_argon)
    )
    if flash_analyzer is not None:
        analyzer_sig = getattr(flash_analyzer, "__qualname__", getattr(flash_analyzer, "__name__", "custom_callable"))
    elif use_agy_for_primary:
        analyzer_sig = f"agy_cli:{resolved_primary_model}"
    else:
        analyzer_sig = f"offline_default:{resolved_primary_model}"

    use_argon_second_pass = bool(
        argon_verifier is not None
        or (use_agy_argon and resolved_primary_model != "gemini-4-argon-high")
    )

    resolved_run_id, run_config = compute_multimodal_run_id(
        primary_model=resolved_primary_model,
        primary_model_mode=normalized_mode,
        use_agy_argon_verifier=use_argon_second_pass,
        prompt_signature=prompt_signature,
        analyzer_signature=analyzer_sig,
        panels_per_board=panels_per_board,
        max_keyframes=max_keyframes,
        crop_box=crop_box,
        board_paths=board_paths,
        roi_mode=roi_mode,
        run_id=run_id,
    )

    ckpt = MultimodalCheckpointManager(
        root,
        video_id,
        run_id=resolved_run_id,
        run_config=run_config,
    )
    sync_top_level = _can_sync_top_level_artifacts(vdir, resolved_run_id)

    if force or not ckpt.is_stage_done("media_cached"):
        ckpt.mark_stage_done("media_cached", artifact_path=cache_file, metadata={"asset_count": len(manifest["assets"])})
    if force or not ckpt.is_stage_done("subtitles_parsed"):
        ckpt.mark_stage_done(
            "subtitles_parsed",
            artifact_path=parsed_sub_path,
            metadata={"valid_segment_count": parsed_subtitles["valid_segment_count"]},
        )
    if force or not ckpt.is_stage_done("metadata_probed"):
        ckpt.mark_stage_done("metadata_probed", artifact_path=meta_path, metadata={"backend": media_metadata["probe_backend"]})
    if force or not ckpt.is_stage_done("scenes_detected"):
        ckpt.mark_stage_done("scenes_detected", artifact_path=scenes_path, metadata={"scene_count": scenes_payload["scene_count"]})

    # Stage 5: Full-frame 4K evidence with optional focused UI ROI companions
    # All V3 outputs are run-scoped; NEVER reuse legacy top-level cropped frames.
    is_default_board_config = False
    board_base_dir = vdir if is_default_board_config else ckpt.run_dir
    frames_manifest_path = board_base_dir / "frames_manifest.json"

    if (force and not (is_default_board_config and has_protected_legacy)) or not ckpt.is_stage_done("frames_extracted"):
        if is_default_board_config and frames_manifest_path.exists() and not (force and not has_protected_legacy):
            frames_payload = read_json(frames_manifest_path)
        else:
            ui_timestamps = select_curated_ui_timestamps(
                scenes_payload.get("scenes", []),
                parsed_subtitles,
                max_keyframes=max_keyframes,
                min_gap_sec=4.5,
            )
            if not ui_timestamps and scenes_payload.get("scenes"):
                ui_timestamps = [float(scenes_payload["scenes"][0]["keyframe_sec"])]
            frame_source = cached_highres_path if (cached_highres_path and cached_highres_path.exists()) else cached_video_path
            frames_extracted = extract_highres_ui_frames(
                frame_source,
                board_base_dir / "frames",
                ui_timestamps,
                crop_box=crop_box,
            )
            # Pixel-level deduplication prevents adjacent screenshots showing
            # the same static UI twice; originals stay on disk for audit.
            frames_extracted, duplicate_frames = deduplicate_visual_frames(frames_extracted)
            boards_built = build_visual_evidence_boards(
                frames_extracted,
                board_base_dir / "boards",
                panels_per_board=panels_per_board,
            )
            roi_frames: List[Dict[str, Any]] = []
            roi_boards: List[Dict[str, Any]] = []
            if roi_mode == "auto" and not board_paths and frames_extracted:
                # Four distributed attention points at most, with three
                # overlapping ROIs each; the full 4K frames are always kept.
                focus_count = min(4, len(frames_extracted))
                focus = [
                    frames_extracted[min(len(frames_extracted) - 1,
                        int((i + 0.5) * len(frames_extracted) / focus_count))]
                    for i in range(focus_count)
                ]
                focus_ts = [float(f["actual_sec"]) for f in focus]
                for roi_name, roi_box in ROI_BOXES.items():
                    crops = extract_highres_ui_frames(
                        frame_source,
                        board_base_dir / "roi" / roi_name,
                        focus_ts,
                        crop_box=roi_box,
                        prefix="roi_" + roi_name,
                    )
                    for cropped in crops:
                        cropped["roi_name"] = roi_name
                        cropped["label"] = "ROI " + roi_name
                    roi_frames.extend(crops)
                roi_boards = build_visual_evidence_boards(
                    roi_frames,
                    board_base_dir / "boards",
                    panels_per_board=panels_per_board,
                    prefix="roi_board",
                )
            frames_payload = {
                "video_id": video_id,
                "board_config": run_config["evidence_board_config"],
                "frame_count": len(frames_extracted),
                "frames": frames_extracted,
                "raw_frame_count": len(frames_extracted) + len(duplicate_frames),
                "duplicate_rejections": duplicate_frames,
                "roi_frame_count": len(roi_frames),
                "roi_frames": roi_frames,
                "board_count": len(boards_built) + len(roi_boards),
                "boards": boards_built + roi_boards,
            }
            atomic_write(frames_manifest_path, json_text(frames_payload))
        ckpt.mark_stage_done(
            "frames_extracted",
            artifact_path=frames_manifest_path,
            metadata={"frame_count": frames_payload["frame_count"], "board_count": frames_payload["board_count"]},
        )
    else:
        frames_payload = read_json(frames_manifest_path)

    # Stage 6: Video-Subtitle time-axis alignment
    aligned_path = board_base_dir / "aligned_timeline.json"
    if (force and not (is_default_board_config and has_protected_legacy)) or not ckpt.is_stage_done("timeline_aligned"):
        if is_default_board_config and aligned_path.exists() and not (force and not has_protected_legacy):
            aligned_timeline = read_json(aligned_path)
        else:
            aligned_timeline = align_multimodal_timeline(
                scenes_payload.get("scenes", []),
                parsed_subtitles,
                extracted_frames=frames_payload.get("frames", []),
            )
            atomic_write(aligned_path, json_text(aligned_timeline))
        ckpt.mark_stage_done("timeline_aligned", artifact_path=aligned_path, metadata={"total_scenes": aligned_timeline["total_scenes"]})
    else:
        aligned_timeline = read_json(aligned_path)

    # Stage 7: Primary model analysis (Argon independent / Flash independent / Auto policy) + conditional verification
    analysis_path = ckpt.run_dir / "tiered_analysis.json"
    captured_usages: Dict[str, Dict[str, Any]] = {}
    if force or not ckpt.is_stage_done("tiered_analyzed"):
        active_board_paths = list(board_paths or [b["path"] for b in frames_payload.get("boards", [])])
        if flash_analyzer is not None:
            default_primary = flash_analyzer
        elif use_agy_for_primary:
            def _run_primary_agy(tl: Dict[str, Any]) -> Dict[str, Any]:
                out, usage = run_agy_flash_multimodal_analyzer(
                    root,
                    video_id,
                    tl,
                    board_paths=active_board_paths,
                    parsed_subtitles=parsed_subtitles,
                    evidence_manifest=frames_payload,
                    model=resolved_primary_model,
                    effort=primary_effort,
                    run_label=resolved_run_id,
                )
                captured_usages["primary"] = usage
                return out
            default_primary = _run_primary_agy
        else:
            default_primary = lambda tl: {
                "scene_analyses": [
                    {
                        "scene_id": u["scene_id"],
                        "start_sec": u["start_sec"],
                        "end_sec": u["end_sec"],
                        "summary": u["combined_transcript_text"][:200] or f"Scene {u['scene_id']}",
                        "spoken_summary": u["combined_transcript_text"],
                        "visual_ui_text": "",
                        "confidence": 0.92,
                        "verification_status": "VERIFIED",
                        "verified_by": resolved_primary_model,
                    }
                    for u in tl.get("units", [])
                ],
                "prompts_extracted": [],
                "ui_settings_extracted": [],
            }

        active_argon = argon_verifier
        if active_argon is None and use_agy_argon and resolved_primary_model != "gemini-4-argon-high":
            def _run_verifier_agy(unc: List[Dict[str, Any]], tl: Dict[str, Any]) -> Dict[str, Any]:
                out, usage = run_agy_argon_selective_verifier(
                    root,
                    video_id,
                    unc,
                    tl,
                    frame_paths=active_board_paths[:4],
                    run_label=f"{resolved_run_id}_argon_verifier",
                )
                captured_usages["verifier"] = usage
                return out
            active_argon = _run_verifier_agy

        analysis_result = run_tiered_multimodal_analysis(
            aligned_timeline,
            flash_analyzer=default_primary,
            selective_highres_reanalyzer=selective_highres_reanalyzer,
            argon_verifier=active_argon,
            primary_model=resolved_primary_model,
        )
        # Fail closed on references missing from the manifest. PANEL numbers
        # follow the actual board renderer, not the frame_id suffix.
        # Existing V3 ROI references #05..#12 are correct and remain unchanged.
        analysis_result, evidence_validation = reconcile_analysis_evidence(
            analysis_result, frames_payload,
        )
        analysis_result["evidence_reference_validation"] = evidence_validation
        # Explicit chapter IDs separate logical chapters from detected scenes.
        analysis_result = attach_chapter_provenance(analysis_result, aligned_timeline)
        analysis_result = classify_evidence_items(analysis_result)
        analysis_result["ui_settings_extracted"] = [
            dict(
                u,
                model_reported_status=u.get("status", "UNVERIFIED"),
                status=("UNVERIFIED" if str(u.get("status", "")).upper()
                        in {"UNVERIFIED", "UNCERTAIN", "CONFLICT", "PARTIAL"}
                        else "SELF_CHECKED"),
            )
            for u in analysis_result.get("ui_settings_extracted", []) or []
        ]
        analysis_result["run_id"] = resolved_run_id
        analysis_result["run_config"] = run_config
        if "primary" in captured_usages:
            cost_summary = separate_workflow_costs(
                captured_usages["primary"],
                verifier_usage=captured_usages.get("verifier"),
                root=root,
            )
            analysis_result["cost_summary"] = cost_summary
        atomic_write(analysis_path, json_text(analysis_result))
        if sync_top_level:
            atomic_write(vdir / "tiered_analysis.json", json_text(analysis_result))
        ckpt.mark_stage_done(
            "tiered_analyzed",
            artifact_path=analysis_path,
            metadata={
                "primary_model": resolved_primary_model,
                "argon_invoked": analysis_result["argon_invoked"],
                "run_id": resolved_run_id,
            },
        )
    else:
        analysis_result = read_json(analysis_path)

    # Stage 8: Structured verification persistence
    ver_path = ckpt.run_dir / "verification.json"
    if force or not ckpt.is_stage_done("verification_saved"):
        # Never copy primary-model text into ground_truth or label a
        # primary Argon response as an independent Flash/Argon review.
        ver_items = build_provenance_verification_items(
            analysis_result, primary_model=resolved_primary_model,
        )
        for item in ver_items:
            item["timestamp"] = format_mmss(float(item["timestamp"]))
        verification_report = save_verification_results(
            root,
            video_id,
            ver_items,
            summary_stats={"run_id": resolved_run_id, "primary_model": resolved_primary_model},
            output_path=ver_path,
        )
        if sync_top_level:
            atomic_write(verification_path(root, video_id), json_text(verification_report))
        ckpt.mark_stage_done("verification_saved", artifact_path=ver_path, metadata={"total_items": verification_report["total_items"]})
    else:
        verification_report = read_json(ver_path)

    # Stage 9: Code-based final Markdown assembly
    md_path = ckpt.run_dir / "final_report.md"
    if force or not ckpt.is_stage_done("markdown_assembled"):
        md_text = assemble_final_markdown(
            video_id=video_id,
            title=title or f"YouTube Research {video_id}",
            url=url or f"https://www.youtube.com/watch?v={video_id}",
            media_metadata=media_metadata,
            analysis_result=analysis_result,
            parsed_subtitles=parsed_subtitles,
            verification_report=verification_report,
        )
        atomic_write(md_path, md_text)
        if sync_top_level:
            atomic_write(vdir / "final_report.md", md_text)
        ckpt.mark_stage_done("markdown_assembled", artifact_path=md_path, metadata={"markdown_chars": len(md_text)})
    else:
        md_text = md_path.read_text(encoding="utf-8")

    final_ckpt_state = ckpt.load()
    if sync_top_level:
        atomic_write(vdir / "multimodal_checkpoint.json", json_text(final_ckpt_state))

    return {
        "video_id": video_id,
        "run_id": resolved_run_id,
        "run_config": run_config,
        "primary_model": resolved_primary_model,
        "primary_model_mode": normalized_mode,
        "checkpoint_path": str(ckpt.path),
        "checkpoint": final_ckpt_state,
        "report_path": str(md_path),
        "analysis_path": str(analysis_path),
        "verification_path": str(ver_path),
        "argon_invoked": analysis_result.get("argon_invoked", False),
        "valid_segment_count": parsed_subtitles.get("valid_segment_count", 0),
        "scene_count": aligned_timeline.get("total_scenes", 0),
    }


# ---------------------------------------------------------------------------
# 13. Per-Run Actual Token Usage Accounting & Persistence
# ---------------------------------------------------------------------------

def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def parse_agy_session_transcript_usage(
    transcript_jsonl_path: Path,
    *,
    start_step: int = 0,
    end_step: Optional[int] = None,
    split_by_user_turns: bool = True,
) -> Dict[str, Any]:
    """Parse exact token, cache, tool, and media metrics from an AGY session transcript.jsonl.

    Prevents double-counting between uncached input_tokens and cache_read_tokens,
    separates distinct USER_INPUT turns within the same session, and records unmeasured
    fields as 'UNKNOWN'.
    """
    path = Path(transcript_jsonl_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"TRANSCRIPT_LOG_NOT_FOUND: {path}")

    steps: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                steps.append(json.loads(stripped))

    max_idx = max((int(s.get("step_index", 0)) for s in steps), default=0)
    upper = max_idx if end_step is None else int(end_step)
    filtered = [s for s in steps if int(start_step) <= int(s.get("step_index", -1)) <= upper]

    def _summarize_slice(slice_steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not slice_steps:
            return {}
        t0 = _parse_iso_dt(slice_steps[0].get("created_at"))
        t1 = _parse_iso_dt(slice_steps[-1].get("created_at"))
        duration_seconds = round((t1 - t0).total_seconds(), 3) if (t0 and t1) else None

        planner_steps = [s for s in slice_steps if s.get("type") == "PLANNER_RESPONSE"]
        uncached_input_sum = 0
        cache_read_sum = 0
        output_sum = 0
        thinking_present = False
        thinking_sum = 0
        cache_create_present = False
        cache_create_sum = 0

        tool_calls_total = 0
        tool_counts: Dict[str, int] = {}
        media_inputs_count = 0
        media_uris: List[str] = []
        failed_tool_steps: List[int] = []
        context_sizes: List[int] = []

        for s in slice_steps:
            if s.get("media"):
                for m in s.get("media") or []:
                    media_inputs_count += 1
                    if isinstance(m, dict) and m.get("uri"):
                        media_uris.append(str(m["uri"]))
            if s.get("status") == "ERROR":
                failed_tool_steps.append(int(s.get("step_index", -1)))
            elif s.get("type") not in ("PLANNER_RESPONSE", "USER_INPUT"):
                content_str = str(s.get("content", ""))
                if "Command exited with code 1" in content_str or "Encountered error" in content_str:
                    failed_tool_steps.append(int(s.get("step_index", -1)))

            if s.get("type") == "PLANNER_RESPONSE":
                inp = int(s.get("input_tokens", 0) or 0)
                cr = int(s.get("cache_read_tokens", 0) or 0)
                out = int(s.get("output_tokens", 0) or 0)
                uncached_input_sum += inp
                cache_read_sum += cr
                output_sum += out
                context_sizes.append(inp + cr)

                if "thinking_tokens" in s and s.get("thinking_tokens") is not None:
                    thinking_present = True
                    thinking_sum += int(s["thinking_tokens"])
                if "cache_creation_tokens" in s and s.get("cache_creation_tokens") is not None:
                    cache_create_present = True
                    cache_create_sum += int(s["cache_creation_tokens"])

                for tc in s.get("tool_calls") or []:
                    tool_calls_total += 1
                    tname = str(tc.get("name", "unknown"))
                    tool_counts[tname] = tool_counts.get(tname, 0) + 1

        return {
            "step_range": [int(slice_steps[0].get("step_index", 0)), int(slice_steps[-1].get("step_index", 0))],
            "planner_calls": len(planner_steps),
            "duration_seconds": duration_seconds,
            "uncached_input_tokens": uncached_input_sum,
            "cache_read_tokens": cache_read_sum,
            "cache_creation_tokens": cache_create_sum if cache_create_present else "UNKNOWN",
            "output_tokens": output_sum,
            "thinking_tokens": thinking_sum if thinking_present else "UNKNOWN",
            "total_tokens_uncached_plus_output": uncached_input_sum + output_sum,
            "total_prompt_context_tokens_with_cache": uncached_input_sum + cache_read_sum,
            "total_all_tokens_with_cache": uncached_input_sum + cache_read_sum + output_sum,
            "first_step_context_tokens": context_sizes[0] if context_sizes else 0,
            "peak_step_context_tokens": max(context_sizes) if context_sizes else 0,
            "final_step_context_tokens": context_sizes[-1] if context_sizes else 0,
            "net_context_accumulation_tokens": (context_sizes[-1] - context_sizes[0]) if len(context_sizes) >= 2 else 0,
            "tool_calls_total": tool_calls_total,
            "tool_counts_by_name": tool_counts,
            "media_input_count": media_inputs_count,
            "media_uris": media_uris,
            "failed_or_retried_step_indices": sorted(set(failed_tool_steps)),
            "failed_or_retried_count": len(set(failed_tool_steps)),
        }

    overall = _summarize_slice(filtered)
    turns: List[Dict[str, Any]] = []
    if split_by_user_turns and filtered:
        user_indices = [i for i, s in enumerate(filtered) if s.get("type") == "USER_INPUT"]
        if not user_indices or user_indices[0] != 0:
            user_indices = [0] + user_indices
        for t_idx, start_pos in enumerate(user_indices, start=1):
            end_pos = user_indices[t_idx] if t_idx < len(user_indices) else len(filtered)
            turn_slice = filtered[start_pos:end_pos]
            turn_summary = _summarize_slice(turn_slice)
            turn_summary["turn_number"] = t_idx
            turns.append(turn_summary)

    overall["turns"] = turns
    overall["source_log"] = str(path)
    return overall


DEFAULT_PRICING_CONFIG: Dict[str, Any] = {
    "schema_version": 1,
    "pricing_version": "2026-11-06.v1",
    "effective_date": "2026-11-06",
    "source_name": "Google Cloud Model Garden Standard Global Pricing",
    "source_url": "https://cloud.google.com/model-garden/pricing",
    "currency": "USD",
    "unit_tokens": 1000000,
    "agy_quota_deduction_default": "UNKNOWN",
    "models": {
        "gemini-3.8-flash-medium": {
            "family": "Flash 3.8",
            "input_per_1m_usd": 0.75,
            "output_per_1m_usd": 3.75,
            "cached_input_per_1m_usd": 0.075,
            "output_includes_thinking": True,
        },
        "gemini-3.8-flash": {
            "family": "Flash 3.8",
            "input_per_1m_usd": 0.75,
            "output_per_1m_usd": 3.75,
            "cached_input_per_1m_usd": 0.075,
            "output_includes_thinking": True,
        },
        "gemini-4-argon-high": {
            "family": "Argon 4",
            "input_per_1m_usd": 2.00,
            "output_per_1m_usd": 10.00,
            "cached_input_per_1m_usd": 0.10,
            "output_includes_thinking": True,
        },
        "gemini-4-argon": {
            "family": "Argon 4",
            "input_per_1m_usd": 2.00,
            "output_per_1m_usd": 10.00,
            "cached_input_per_1m_usd": 0.10,
            "output_includes_thinking": True,
        },
    },
}


def load_pricing_config(root: Optional[Path] = None) -> Dict[str, Any]:
    """Load versioned, effective-dated model pricing configuration from config/pricing.json."""
    if root is not None:
        cfg_path = Path(root).resolve() / "config" / "pricing.json"
        if cfg_path.exists():
            return read_json(cfg_path)
    default_repo_cfg = Path(__file__).resolve().parent.parent / "config" / "pricing.json"
    if default_repo_cfg.exists():
        return read_json(default_repo_cfg)
    return json.loads(json.dumps(DEFAULT_PRICING_CONFIG))


def calculate_token_cost(
    usage: Dict[str, Any],
    pricing_config: Optional[Dict[str, Any]] = None,
    *,
    model: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Calculate official API-equivalent cost from raw token usage without mutating raw usage data."""
    cfg = pricing_config or load_pricing_config(root)
    unit_tokens = float(cfg.get("unit_tokens", 1000000) or 1000000)
    models_map = cfg.get("models") or {}

    resolved_model = str(model or usage.get("init_model") or usage.get("model") or "").strip()
    if resolved_model not in models_map:
        if "argon" in resolved_model.lower():
            resolved_model = "gemini-4-argon-high"
        elif "flash" in resolved_model.lower():
            resolved_model = "gemini-3.8-flash-medium"
    rates = models_map.get(resolved_model)
    if not rates:
        raise ValueError(f"PRICING_MODEL_NOT_FOUND: {resolved_model!r}")

    uncached_in = int(usage.get("uncached_input_tokens", usage.get("input_tokens", 0)) or 0)
    cache_read = int(usage.get("cache_read_tokens", 0) or 0)
    output_tok = int(usage.get("output_tokens", 0) or 0)

    in_rate = float(rates["input_per_1m_usd"])
    cache_rate = float(rates["cached_input_per_1m_usd"])
    out_rate = float(rates["output_per_1m_usd"])

    in_cost = (uncached_in * in_rate) / unit_tokens
    cache_cost = (cache_read * cache_rate) / unit_tokens
    out_cost = (output_tok * out_rate) / unit_tokens
    total_cost = in_cost + cache_cost + out_cost

    quota_deduction = usage.get("agy_quota_deduction", cfg.get("agy_quota_deduction_default", "UNKNOWN"))

    return {
        "pricing_version": cfg.get("pricing_version", "2026-11-06.v1"),
        "effective_date": cfg.get("effective_date", "2026-11-06"),
        "source_url": cfg.get("source_url", "https://cloud.google.com/model-garden/pricing"),
        "currency": cfg.get("currency", "USD"),
        "model": resolved_model,
        "rates_per_1m_usd": {
            "input_per_1m_usd": in_rate,
            "cached_input_per_1m_usd": cache_rate,
            "output_per_1m_usd": out_rate,
        },
        "input_cost_usd": round(in_cost, 6),
        "cached_input_cost_usd": round(cache_cost, 6),
        "output_cost_usd": round(out_cost, 6),
        "total_cost_usd": round(total_cost, 6),
        "agy_quota_deduction": quota_deduction if quota_deduction is not None else "UNKNOWN",
    }


def separate_workflow_costs(
    child_usage: Dict[str, Any],
    *,
    verifier_usage: Optional[Dict[str, Any]] = None,
    main_session_usage: Optional[Dict[str, Any]] = None,
    pricing_config: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Separate production batch child operational cost from interactive Main Argon R&D/management cost."""
    cfg = pricing_config or load_pricing_config(root)
    child_cost = calculate_token_cost(child_usage, cfg)
    verifier_cost = calculate_token_cost(verifier_usage, cfg) if verifier_usage else None
    main_cost = (
        calculate_token_cost(main_session_usage, cfg, model=main_session_usage.get("model", "gemini-4-argon-high"))
        if main_session_usage
        else None
    )

    batch_op_usd = child_cost["total_cost_usd"] + (verifier_cost["total_cost_usd"] if verifier_cost else 0.0)
    rd_main_usd = main_cost["total_cost_usd"] if main_cost else 0.0

    return {
        "pricing_version": cfg.get("pricing_version", "2026-11-06.v1"),
        "effective_date": cfg.get("effective_date", "2026-11-06"),
        "source_url": cfg.get("source_url", "https://cloud.google.com/model-garden/pricing"),
        "batch_operational_child_cost_usd": round(child_cost["total_cost_usd"], 6),
        "batch_operational_verifier_cost_usd": round(verifier_cost["total_cost_usd"], 6) if verifier_cost else 0.0,
        "batch_operational_total_cost_usd": round(batch_op_usd, 6),
        "rd_management_main_session_cost_usd": round(rd_main_usd, 6),
        "total_observed_workflow_cost_usd": round(batch_op_usd + rd_main_usd, 6),
        "main_session_invoked_in_batch_mode": False,
        "agy_quota_deduction": "UNKNOWN",
        "breakdown": {
            "child": child_cost,
            "verifier": verifier_cost,
            "main_session_rd": main_cost,
        },
    }


EMBEDDED_HINT_SIGNATURES = (
    "ChatGPT lecture for 25 million professionals",
    "Career Hacker Alex...",
    "전문적, 자신감 있음, 유능한",
)


def classify_run_independence(
    model: str,
    *,
    prompt_text: str = "",
    has_embedded_ground_truth: bool = False,
) -> str:
    """Classify a run as ARGON_ASSISTED, INDEPENDENT_FLASH, or INDEPENDENT_ARGON."""
    if has_embedded_ground_truth or any(sig in prompt_text for sig in EMBEDDED_HINT_SIGNATURES):
        return "ARGON_ASSISTED"
    m_lower = str(model or "").lower()
    if "argon" in m_lower:
        return "INDEPENDENT_ARGON"
    if "flash" in m_lower:
        return "INDEPENDENT_FLASH"
    return "INDEPENDENT_UNKNOWN"


def verify_child_runtime_model(
    raw_stdout_or_usage: str | Dict[str, Any],
    expected_model: str,
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    """Verify the actual child process runtime model from stream-json init.model."""
    if isinstance(raw_stdout_or_usage, str):
        parsed = parse_agy_stream_json_usage(raw_stdout_or_usage, expected_model=expected_model)
        actual = str(parsed.get("init_model") or parsed.get("model") or "UNKNOWN")
    else:
        actual = str(raw_stdout_or_usage.get("init_model") or raw_stdout_or_usage.get("model") or "UNKNOWN")

    verified = (actual == expected_model)
    if strict and not verified:
        raise RuntimeError(
            f"AGY_CHILD_MODEL_MISMATCH: expected={expected_model!r}, actual_init_model={actual!r}"
        )
    return {
        "expected_model": expected_model,
        "init_model": actual,
        "model_verified": verified,
    }


def parse_agy_stream_json_usage(
    raw_stdout: str,
    *,
    expected_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse exact token usage from an AGY CLI stream-json stdout payload.

    Explicitly prevents double-counting between individual step_update.usage
    and the terminal result.usage summary, and extracts init.model for runtime verification.
    """
    step_usages: List[Dict[str, Any]] = []
    result_usage: Optional[Dict[str, Any]] = None
    duration_seconds: Optional[float] = None
    model_name: Optional[str] = None
    tool_calls_total = 0
    failed_tool_calls = 0
    tool_counts: Dict[str, int] = {}
    media_input_count = 0
    media_paths: List[str] = []

    media_exts = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".m4a", ".wav", ".webm")

    for line in raw_stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except Exception:
            continue
        event = obj.get("event")
        if event == "init":
            model_name = (obj.get("init") or {}).get("model")
        elif event == "step_update":
            su = obj.get("step_update") or {}
            stype = su.get("step_type")
            state = su.get("state")
            if stype in ("tool", "tool_call", "subagent"):
                if state == "DONE":
                    tool_calls_total += 1
                    tinfo = su.get("tool_info") or {}
                    tname = str(su.get("tool_name") or tinfo.get("name") or "unknown")
                    tool_counts[tname] = tool_counts.get(tname, 0) + 1
                    params = tinfo.get("parameters") or {}
                    path_arg = str(params.get("AbsolutePath") or params.get("Path") or "")
                    if path_arg and path_arg.lower().endswith(media_exts):
                        media_input_count += 1
                        media_paths.append(path_arg)
                elif state == "ERROR":
                    tool_calls_total += 1
                    failed_tool_calls += 1
            if state == "DONE" and su.get("usage"):
                step_usages.append(dict(su["usage"]))
        elif event == "result":
            res = obj.get("result") or {}
            duration_seconds = res.get("duration_seconds")
            if res.get("usage"):
                result_usage = dict(res["usage"])

    uncached_input = sum(int(u.get("input_tokens", 0) or 0) for u in step_usages)
    cache_read = sum(int(u.get("cache_read_tokens", 0) or 0) for u in step_usages)
    output_tokens = sum(int(u.get("output_tokens", 0) or 0) for u in step_usages)
    thinking_tokens = sum(int(u.get("thinking_tokens", 0) or 0) for u in step_usages)

    if result_usage and not step_usages:
        uncached_input = int(result_usage.get("input_tokens", 0) or 0)
        cache_read = int(result_usage.get("cache_read_tokens", 0) or 0)
        output_tokens = int(result_usage.get("output_tokens", 0) or 0)
        thinking_tokens = int(result_usage.get("thinking_tokens", 0) or 0)

    result_matches_step_sum = True
    if result_usage and step_usages:
        res_in = int(result_usage.get("input_tokens", 0) or 0)
        res_out = int(result_usage.get("output_tokens", 0) or 0)
        result_matches_step_sum = (res_in == uncached_input and res_out == output_tokens)

    visible_output = max(0, output_tokens - thinking_tokens)
    total_excluding_cache = uncached_input + output_tokens
    total_with_cache = uncached_input + cache_read + output_tokens
    resolved_init_model = model_name or "UNKNOWN"

    payload: Dict[str, Any] = {
        "model": resolved_init_model,
        "init_model": resolved_init_model,
        "planner_calls": len(step_usages) if step_usages else (1 if result_usage else 0),
        "duration_seconds": round(float(duration_seconds), 3) if duration_seconds is not None else "UNKNOWN",
        "input_tokens": uncached_input,
        "uncached_input_tokens": uncached_input,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": "UNKNOWN",
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "visible_output_tokens": visible_output,
        "output_includes_thinking": True,
        "total_tokens": total_with_cache,
        "total_tokens_excluding_cache": total_excluding_cache,
        "total_prompt_context_tokens": uncached_input + cache_read,
        "tool_calls_total": tool_calls_total,
        "failed_tool_calls": failed_tool_calls,
        "tool_counts_by_name": tool_counts,
        "media_input_count": media_input_count,
        "media_paths": media_paths,
        "double_counting_prevented": True,
        "result_matches_step_sum": result_matches_step_sum,
        "agy_quota_deduction": "UNKNOWN",
    }
    if expected_model is not None:
        payload["expected_model"] = expected_model
        payload["model_verified"] = (resolved_init_model == expected_model)
    return payload


def save_run_token_usage(
    root: Path,
    video_id: str,
    run_usage: Dict[str, Any],
    *,
    run_label: str = "default",
) -> Path:
    """Append or save per-run actual token usage to data/videos/<video_id>/token_usage.json."""
    vdir = video_dir(root, video_id)
    vdir.mkdir(parents=True, exist_ok=True)
    ledger_path = vdir / "token_usage.json"
    existing: Dict[str, Any] = {"schema_version": 1, "video_id": video_id, "runs": []}
    if ledger_path.exists():
        try:
            existing = read_json(ledger_path)
        except Exception:
            pass

    entry = dict(run_usage)
    entry["run_label"] = run_label
    entry["recorded_utc"] = now_iso()
    runs = list(existing.get("runs", []))
    runs.append(entry)
    existing["runs"] = runs
    existing["updated_utc"] = now_iso()
    atomic_write(ledger_path, json_text(existing))
    return ledger_path


# ---------------------------------------------------------------------------
# 14. Real AGY CLI Multimodal Analyzer (Flash Default + Selective Argon)
# ---------------------------------------------------------------------------

MULTIMODAL_ANALYSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["scene_analyses", "prompts_extracted", "ui_settings_extracted"],
    "properties": {
        "scene_analyses": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "scene_id",
                    "start_sec",
                    "end_sec",
                    "summary",
                    "visual_ui_text",
                    "spoken_summary",
                    "confidence",
                    "verification_status",
                ],
                "properties": {
                    "scene_id": {"type": "integer"},
                    "start_sec": {"type": "number"},
                    "end_sec": {"type": "number"},
                    "summary": {"type": "string"},
                    "visual_ui_text": {"type": "string"},
                    "spoken_summary": {"type": "string"},
                    "confidence": {"type": "number"},
                    "verification_status": {"type": "string"},
                    "verified_by": {"type": "string"},
                    "evidence_ref": {"type": "string"},
                },
            },
        },
        "prompts_extracted": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label", "timestamp", "verbatim_text"],
                "properties": {
                    "label": {"type": "string"},
                    "timestamp": {"type": "string"},
                    "verbatim_text": {"type": "string"},
                    "evidence_ref": {"type": "string"},
                },
            },
        },
        "ui_settings_extracted": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["timestamp", "setting_name", "selected_value", "status", "evidence_ref"],
                "properties": {
                    "timestamp": {"type": "string"},
                    "setting_name": {"type": "string"},
                    "selected_value": {"type": "string"},
                    "status": {"type": "string"},
                    "evidence_ref": {"type": "string"},
                },
            },
        },
        "research_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["section_id", "text", "timestamp", "claim_origin"],
                "properties": {
                    "section_id": {"type": "integer", "minimum": 1, "maximum": 18},
                    "text": {"type": "string"},
                    "timestamp": {"type": "string"},
                    "claim_origin": {
                        "type": "string",
                        "enum": ["SPEAKER_STATEMENT", "OBSERVED_UI",
                                 "OBSERVED_USER_ACTION", "AI_GENERATED_OUTPUT",
                                 "GENERATED_SLIDE", "SOURCE_DOCUMENT",
                                 "MIXED_SOURCES"],
                    },
                    "source_context": {"type": "string"},
                    "evidence_ref": {"type": "string"},
                    "source_segment_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "workflow_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["timestamp", "action", "interaction_type"],
                "properties": {
                    "timestamp": {"type": "string"}, "action": {"type": "string"},
                    "interaction_type": {"type": "string",
                        "enum": ["MANUAL_UI", "PRODUCT_GENERATED", "CLI_EXECUTION"]},
                    "observed_result": {"type": "string"},
                    "evidence_ref": {"type": "string"},
                    "source_segment_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "cli_commands": {
            "type": "array", "items": {"type": "object", "properties": {
                "command": {"type": "string"}, "timestamp": {"type": "string"},
                "evidence_ref": {"type": "string"},
                "source_segment_ids": {"type": "array", "items": {"type": "string"}},
            }},
        },
    },
}


def _extract_json_from_agy_stdout(stdout: str) -> Dict[str, Any]:
    """Extract and schema-validate the structured JSON payload from an AGY stream-json stdout."""
    from .agent import parse_agy_envelope, parse_json_documents, require_schema_shape

    envelope = parse_agy_envelope(stdout)
    if envelope.get("status") != "SUCCESS":
        raise RuntimeError(f"AGY_MULTIMODAL_NON_SUCCESS: status={envelope.get('status')!r}")
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        require_schema_shape(structured, MULTIMODAL_ANALYSIS_SCHEMA)
        return structured
    response_text = str(envelope.get("response") or "").strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)
    # Handle cases where model prepends brief text before '{'
    first_brace = response_text.find("{")
    last_brace = response_text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        response_text = response_text[first_brace:last_brace + 1]
    docs = parse_json_documents(response_text)
    result_doc = docs[-1]
    require_schema_shape(result_doc, MULTIMODAL_ANALYSIS_SCHEMA)
    return result_doc


def _build_logical_chapters(aligned_timeline: Dict[str, Any], target_chapters: int = 12) -> List[Dict[str, Any]]:
    """Group fine-grained scene intervals into ~12 logical chronological chapters to prevent output token bloat."""
    units = aligned_timeline.get("units", []) or []
    if not units:
        return []
    if len(units) <= target_chapters:
        return [
            {
                "scene_id": idx,
                "chapter_id": idx,
                "source_scene_ids": [int(u["scene_id"])],
                "start_sec": float(u["start_sec"]),
                "end_sec": float(u["end_sec"]),
                "start_mmss": format_mmss(float(u["start_sec"])),
                "end_mmss": format_mmss(float(u["end_sec"])),
                "segment_ids": u.get("segment_ids", []),
            }
            for idx, u in enumerate(units, start=1)
        ]
    chunk_size = max(1, (len(units) + target_chapters - 1) // target_chapters)
    chapters: List[Dict[str, Any]] = []
    for c_idx, i in enumerate(range(0, len(units), chunk_size), start=1):
        group = units[i:i + chunk_size]
        seg_ids: List[str] = []
        for g in group:
            seg_ids.extend(g.get("segment_ids", []))
        chapters.append({
            "scene_id": c_idx,
            "chapter_id": c_idx,
            "source_scene_ids": [int(g["scene_id"]) for g in group],
            "start_sec": float(group[0]["start_sec"]),
            "end_sec": float(group[-1]["end_sec"]),
            "start_mmss": format_mmss(float(group[0]["start_sec"])),
            "end_mmss": format_mmss(float(group[-1]["end_sec"])),
            "segment_ids": seg_ids,
        })
    return chapters


def run_agy_flash_multimodal_analyzer(
    root: Path,
    video_id: str,
    aligned_timeline: Dict[str, Any],
    *,
    board_paths: Sequence[Path | str],
    parsed_subtitles: Optional[Dict[str, Any]] = None,
    evidence_manifest: Optional[Dict[str, Any]] = None,
    model: str = "gemini-3.8-flash-medium",
    effort: str = "medium",
    agent_name: str = "yt-research-multimodal",
    run_label: str = "flash_optimized_e2e",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Execute Gemini 3.8 Flash via AGY CLI on curated high-res visual evidence boards + JSON3 timeline."""
    from .agent import Agy
    from .diagnostics import archive_agy_attempt

    runner = Agy(root)
    abs_boards: List[str] = [str(Path(bp).resolve()).replace("\\", "/") for bp in board_paths]

    sub_lines: List[str] = []
    if parsed_subtitles and parsed_subtitles.get("segments"):
        for seg in parsed_subtitles["segments"]:
            sub_lines.append(f"[{seg['id']} {seg['timestamp']}] {seg['text']}")
    else:
        for u in aligned_timeline.get("units", []):
            for seg in u.get("subtitles", []):
                sub_lines.append(f"[{seg['segment_id']} {seg['timestamp']}] {seg['text']}")

    chapters = _build_logical_chapters(aligned_timeline, target_chapters=12)
    chapter_lines = [
        f"- scene_id={ch['scene_id']}: {ch['start_sec']:.1f}s ~ {ch['end_sec']:.1f}s ({ch['start_mmss']}~{ch['end_mmss']})"
        for ch in chapters
    ]

    boards_listing = "\n".join(f"- {ab}" for ab in abs_boards)
    catalog_lines: List[str] = []
    if evidence_manifest:
        panel_catalog = build_panel_catalog(evidence_manifest)
        for board_name, board in panel_catalog.items():
            for index, meta in sorted(board["panels"].items()):
                catalog_lines.append(
                    f"- {board_name} (PANEL #{index:02d}) : "
                    f"frame_id={meta['frame_id']}; timestamp_sec={meta['timestamp_sec']}"
                )
    evidence_catalog_text = "\n".join(catalog_lines)
    n_boards = len(abs_boards)
    n_chapters = len(chapters)
    role_tag = "multimodal_argon" if "argon" in model.lower() else "multimodal_flash"
    prompt = (
        f"[멀티모달 기술 교육 영상 정밀 분석 요청: 영상 ID {video_id}]\n"
        f"아래 1) 원본 전체 프레임 및 추가 좌·중·우 ROI 증거 보드 {n_boards}장과 2) 코드 기반 JSON3 정렬 자막을 결합하여 "
        f"전체 영상을 {n_chapters}개 논리 챕터(`scene_id`: 1~{n_chapters})로 정밀 분석하세요.\n"
        "기존 분석 문서나 외부 지식을 절대 참조하지 말고, 오직 제공된 4K 보드 이미지와 JSON3 자막 원문만 근거로 삼으세요.\n\n"
        "[필수 도구 실행 지침 — 엄격 준수]\n"
        f"1. 아래 {n_boards}개 보드 이미지 파일의 절대 경로를 `view_file` (`read_file`)로 모두 열어 확인하세요. 한 턴에 최대 10개씩 나누어 읽고 누락하지 마세요:\n"
        f"{boards_listing}\n"
        "[정확한 증거 참조 카탈로그: 생성기가 frame_id에서 계산함]\n"
        f"{evidence_catalog_text}\n"
        "증거의 이미지 파일명·PANEL 번호는 반드시 카탈로그의 같은 행을 그대로 사용하세요. "
        "ROI 패널 번호는 roi_board_01부터 ROI 전체에 걸쳐 연속으로 부여됩니다. " 
        "각 이미지 헤더와 위 카탈로그에 적힌 번호를 그대로 사용하세요.\n"
        "2. 위 `.png` 파일 외에 어떠한 `.json`, `.py`, `.md`, `.txt` 파일도 절대 읽지 마세요.\n"
        "3. 모든 이미지의 `view_file` 결과가 반환된 후, 추가 파일 탐색 없이 최종 JSON 객체만 출력하고 종료하세요.\n"
        "4. 모든 서술에서 `[화면]`(보드 이미지에서 직접 판독한 UI 텍스트/프롬프트/설정/메뉴), `[자막]`(JSON3 세그먼트 ID 및 원문), `[음성]`(발화자 설명) 출처를 엄격히 분리하세요.\n\n"
        f"[{n_chapters}개 분석 챕터 정의 (`scene_analyses` 배열은 정확히 아래 {n_chapters}개 `scene_id`로 작성)]\n"
        + "\n".join(chapter_lines) + "\n\n"
        "[필수 기술 정보 추출 요구사항 (`prompts_extracted` 및 `ui_settings_extracted`)]\n"
        "- `prompts_extracted` 배열에는 화면에 실제로 보이는 문자열만 전사하세요. 가려졌거나 잘린 문장은 추측해 복원하지 말고 말줄임 표시로 남기세요:\n"
        "- 각 항목의 label을 USER_PROMPT(사용자 입력), AI_OUTPUT(모델 응답), UI_PLACEHOLDER(입력 안내), GENERATED_CONTENT(생성물), SCREEN_TEXT_UNSPECIFIED(불명확) 중 하나로 시작하세요.\n"
        "  1) 화면 속 AI/LLM 채팅창에 사용자가 입력한 모든 프롬프트 원문 전체 (1차, 2차, 3차 등 각 입력 프롬프트 전문)\n"
        "  2) 각 프롬프트에 대해 AI가 화면에 출력한 응답 리스트 항목들 (예: 추천 주제 목록 1~5번, 슬라이드 구성안 1~10번 등 화면에서 판독 가능한 모든 항목과 본문, 마우스 드래그 선택/하이라이트 범위)\n"
        "  3) 프레젠테이션/문서 도구에서 자동 생성된 개요(Outline) 항목 번호와 제목 전체\n"
        "  4) 이미지/미디어 생성 패널 등에 입력 또는 자동 생성된 영문/국문 프롬프트 원문 전체 (큰따옴표 및 쉼표 포함 글자 그대로)\n"
        "- `ui_settings_extracted` 배열에 포함할 항목 (화면에서 확인되는 모든 UI 설정값·버전·조작 순서):\n"
        "  * 사용된 웹 브라우저 상단 북마크바 항목, 주소창 URL, 좌측 사이드바 메뉴/히스토리/프로필, AI 모델 버전 표기\n"
        "  * 앱 진입 경로 및 선택한 생성 모드/문서 형식\n"
        "  * 프롬프트/문서 편집기 내 모든 옵션 버튼·토글 상태 (선택된 활성 옵션과 비활성 옵션을 모두 명시하고, 패널 간 선택 상태가 변경된 경우 초기값 -> 최종 선택값 변화를 기록)\n"
        "  * 텍스트 입력 필드(대상, 톤 등), 출력 언어, 이미지 소스 설정값, 하단 마크다운/글자수 카운터(`xxx/10000`) 및 카드 수 증감 조절값\n"
        "  * 테마 선택 모달의 탭 필터, 선택된 테마의 정확한 UI 표기명(말줄임표 `...` 등 특수문자 포함), 생성자·업데이트 시간 메타데이터\n"
        "  * 생성 완료된 문서의 제목, 작성자 표기, 좌측 카드 썸네일 네비게이션 번호별 제목, 본문 레이아웃(그리드, 수직 타임라인 1~4 등) 및 우측 플로팅 툴바\n"
        "  * 미디어/이미지 생성 기능의 정확한 UI 진입 경로(문서명 -> 카드명 -> 우측 패널 -> 상위 드롭다운 -> 하위 메뉴 항목 전체) 및 스타일 설정 드롭다운 값들\n"
        "  * 음성 발화(`[음성/자막]`)에서 언급한 모델/API 명칭과 실제 화면(`[화면]`)에 표기된 명칭/버전의 일치·차이 여부 (발화나 화면에 없는 버전 번호를 절대 추론하거나 덧붙이지 말 것)\n\n"
        "[19섹션 연구 보고서용 기술 항목: research_items]\n"
        "영상에서 실제 확인한 기술 정보에 한해서만 1~18번 section_id를 지정하세요. "
        "카테고리: (1)핵심 AI 주제, (2)AI 모델/서비스, (3)Coding Tools, (4)Agent, "
        "(5)Skills, (6)Plugins, (7)MCP, (8)Framework, (9)RAG/Memory, "
        "(10)Prompt Engineering, (11)API, (12)Automation, (13)CLI/설정, "
        "(14)Workflow, (15)비용/성능, (16)비교, (17)실패/주의, (18)고유 인사이트.\n"
        "각 항목에 text, timestamp, evidence_ref(실제 board명·PANEL), "
        "source_segment_ids(실제 JSON3 s번호), claim_origin을 기재하세요. "
        "claim_origin은 SPEAKER_STATEMENT(발표자 설명), OBSERVED_UI(직접 보이는 UI), "
        "OBSERVED_USER_ACTION(실제 클릭/입력), AI_GENERATED_OUTPUT(채팅 응답), "
        "GENERATED_SLIDE(인포그래픽/슬라이드), SOURCE_DOCUMENT(외부 문서), "
        "MIXED_SOURCES(혼합이 불가피함) 중 하나입니다. "
        "서로 다른 출처의 주장은 가급적 별도 항목으로 나눕니다. "
        "증거 유형이 다르면 같은 주장에서 합치지 마세요. "
        "둘 중 적어도 한 근거가 "
        "없으면 해당 기술 항목을 생성하지 마세요. 등장하지 않은 Skills·MCP·코드·API를 "
        "추론해 채우지 마세요. 원본의 생성 콘텐츠 수치는 현재 사실이 아니라 영상 속 주장입니다. "
        "19번 출처 목록은 Python이 생성합니다.\n"
        "모델 버전 언급은 발표자 발언/생성물 표기/실제 구동 버전으로 분리하세요. "
        "요금제 수치/단어 수 제한/성과 비율은 영상 속 생성물의 주장이지 공식 정책이 아닙니다. "
        "기능이 실제 시연된 경우에만 workflow_steps에 단계별 timestamp, action, "
        "interaction_type(MANUAL_UI/PRODUCT_GENERATED/CLI_EXECUTION), "
        "observed_result, evidence_ref/source_segment_ids를 작성하세요. "
        "원클릭이라는 홍보 문구로 여러 수동 조작을 한 단계로 뭉치지 마세요. "
        "터미널 명령이 실제로 보이지 않으면 cli_commands는 빈 배열입니다. "
        "짤린 입력은 원문 전문으로 재구성하지 않고, UI_PLACEHOLDER는 사용자 입력으로 간주하지 마세요.\n\n"
        "[출력 JSON 형식 규격 (코드 펜스 없이 순수 JSON 객체만 출력)]\n"
        "{\n"
        f'  "scene_analyses": [{{"scene_id": 1, "start_sec": 0.0, "end_sec": 30.0, "summary": "...", "visual_ui_text": "[화면] ...", "spoken_summary": "[자막/음성] ...", "confidence": 0.75, "verification_status": "UNVERIFIED", "verified_by": "{model}", "evidence_ref": "board_01.png (PANEL #01), s000001-s000010"}}],\n'
        '  "prompts_extracted": [{"label": "USER_PROMPT: ...", "timestamp": "00:39", "verbatim_text": "화면에서 실제 보이는 부분...", "evidence_ref": "board_01.png (PANEL #01)"}],\n'
        '  "ui_settings_extracted": [{"timestamp": "02:27", "setting_name": "...", "selected_value": "...", "status": "UNVERIFIED", "evidence_ref": "board_05.png (PANEL #09)"}],\n'
        '  "research_items": [{"section_id": 14, "text": "영상에서 확인한 실습", "timestamp": "02:27", "claim_origin": "OBSERVED_USER_ACTION", "evidence_ref": "board_05.png (PANEL #09)", "source_segment_ids": ["s000001"]}],\n'
        '  "workflow_steps": [{"timestamp": "02:27", "action": "설정 모달 열기", "interaction_type": "MANUAL_UI", "observed_result": "설정 항목 표시", "evidence_ref": "board_05.png (PANEL #09)"}],\n'
        '  "cli_commands": []\n'
        "}\n\n"
        f"[코드 파싱 완료 JSON3 자막 타임라인 ({len(sub_lines)}개 유효 세그먼트)]\n"
        + "\n".join(sub_lines)
    )

    command = [
        runner.binary, "-p", prompt,
        "--model", model,
        "--effort", effort,
        "--agent", agent_name,
        "--output-format", "stream-json",
        "--print-timeout", runner.config.get("print_timeout", "10m"),
        "--disable-slash-commands",
    ]

    started_dt = datetime.now(timezone.utc)
    result = runner._invoke(command, role_tag, 1, 1)
    elapsed_sec = (datetime.now(timezone.utc) - started_dt).total_seconds()

    error: Optional[BaseException] = None
    parsed_output: Dict[str, Any] = {}
    try:
        if result.returncode != 0:
            raise RuntimeError(f"AGY_MULTIMODAL_EXIT_NONZERO: code={result.returncode}")
        parsed_output = _extract_json_from_agy_stdout(result.stdout)
    except BaseException as exc:
        error = exc
        raise
    finally:
        archive_folder = archive_agy_attempt(
            root,
            role=role_tag,
            model=model,
            attempt=1,
            context={"video_id": video_id, "unit": run_label},
            stdout=result.stdout if result else "",
            stderr=result.stderr if result else "",
            exit_code=result.returncode if result else None,
            error=error,
            duration_seconds=elapsed_sec,
            prompt=prompt,
        )

    usage = parse_agy_stream_json_usage(result.stdout, expected_model=model)
    verify_child_runtime_model(usage, model, strict=True)
    usage["independence_mode"] = classify_run_independence(model, prompt_text=prompt)
    usage["execution_architecture"] = "PYTHON_DIRECT_CHILD_CLI"
    usage["main_session_invoked"] = False
    usage["archive_folder"] = str(archive_folder)
    usage["wall_clock_seconds"] = round(elapsed_sec, 3)
    save_run_token_usage(root, video_id, usage, run_label=run_label)
    return parsed_output, usage


def run_agy_argon_selective_verifier(
    root: Path,
    video_id: str,
    uncertain_intervals: Sequence[Dict[str, Any]],
    aligned_timeline: Dict[str, Any],
    *,
    frame_paths: Sequence[Path | str],
    model: str = "gemini-4-argon-high",
    effort: str = "high",
    agent_name: str = "yt-research-multimodal",
    run_label: str = "argon_selective_verifier",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Conditionally invoke Gemini 4 Argon ONLY on uncertain/high-density UI intervals and their 4K crops."""
    from .agent import Agy
    from .diagnostics import archive_agy_attempt

    runner = Agy(root)
    schema_path = root / "data" / "schema_cache" / "multimodal_analysis.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(MULTIMODAL_ANALYSIS_SCHEMA, ensure_ascii=False), encoding="utf-8")

    abs_frames: List[str] = [str(Path(fp).resolve()).replace("\\", "/") for fp in frame_paths]

    frames_listing = "\n".join(f"- {af}" for af in abs_frames)
    intervals_json = json.dumps(list(uncertain_intervals), ensure_ascii=False, indent=2)

    prompt = (
        f"[선택적 고해상도 Argon 교차 검증 요청: 영상 ID {video_id}]\n"
        "전체 영상을 다시 분석하지 마세요. 아래 지정된 고밀도/쟁점 UI 구간과 4K 크롭 프레임만 단일 턴(single turn)에서 "
        "병렬로 `view_file` 호출하여 판독하고, 해당 구간의 정확한 UI 텍스트·프롬프트·설정값을 검증하세요.\n\n"
        "[필수 열람 4K 크롭 이미지 목록 (단일 턴 병렬 view_file 호출)]\n"
        f"{frames_listing}\n\n"
        "[검증 대상 구간 목록]\n"
        f"{intervals_json}\n\n"
        "[출력 JSON 형식 규격 (코드 펜스 없이 순수 JSON 객체만 출력)]\n"
        "{\n"
        '  "scene_analyses": [{"scene_id": 1, "start_sec": 0.0, "end_sec": 30.0, "summary": "...", "visual_ui_text": "[화면] ...", "spoken_summary": "[자막/음성] ...", "confidence": 0.99, "verification_status": "VERIFIED", "verified_by": "gemini-4-argon-high", "evidence_ref": "..."}],\n'
        '  "prompts_extracted": [{"label": "...", "timestamp": "04:46", "verbatim_text": "...", "evidence_ref": "..."}],\n'
        '  "ui_settings_extracted": [{"timestamp": "02:56", "setting_name": "...", "selected_value": "...", "status": "VERIFIED", "evidence_ref": "..."}]\n'
        "}"
    )

    command = [
        runner.binary, "-p", prompt,
        "--model", model,
        "--effort", effort,
        "--agent", agent_name,
        "--output-format", "stream-json",
        "--print-timeout", runner.config.get("print_timeout", "10m"),
        "--disable-slash-commands",
    ]

    started_dt = datetime.now(timezone.utc)
    result = runner._invoke(command, "multimodal_argon", 1, 1)
    elapsed_sec = (datetime.now(timezone.utc) - started_dt).total_seconds()

    error: Optional[BaseException] = None
    parsed_output: Dict[str, Any] = {}
    try:
        if result.returncode != 0:
            raise RuntimeError(f"AGY_ARGON_VERIFIER_EXIT_NONZERO: code={result.returncode}")
        parsed_output = _extract_json_from_agy_stdout(result.stdout)
    except BaseException as exc:
        error = exc
        raise
    finally:
        archive_folder = archive_agy_attempt(
            root,
            role="multimodal_argon",
            model=model,
            attempt=1,
            context={"video_id": video_id, "unit": run_label},
            stdout=result.stdout if result else "",
            stderr=result.stderr if result else "",
            exit_code=result.returncode if result else None,
            error=error,
            duration_seconds=elapsed_sec,
            prompt=prompt,
        )

    usage = parse_agy_stream_json_usage(result.stdout, expected_model=model)
    verify_child_runtime_model(usage, model, strict=True)
    usage["independence_mode"] = "SELECTIVE_ARGON_VERIFIER"
    usage["execution_architecture"] = "PYTHON_DIRECT_CHILD_CLI"
    usage["main_session_invoked"] = False
    usage["archive_folder"] = str(archive_folder)
    usage["wall_clock_seconds"] = round(elapsed_sec, 3)
    save_run_token_usage(root, video_id, usage, run_label=run_label)
    return parsed_output, usage
