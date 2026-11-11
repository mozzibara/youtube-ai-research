"""Compare P0/P1 evidence coverage in two saved multimodal runs, offline.

Do not interpret model-generated text as ground truth. Screen-text omission is
only measured when independent, manually verified source snippets are supplied.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import unicodedata


def load_json(path):
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalize(value):
    return "".join(c.casefold() for c in unicodedata.normalize("NFKC", str(value or "")) if c.isalnum())


def parse_sec(value):
    if isinstance(value, (float, int)):
        return float(value)
    parts = str(value or "0").split(":")
    try:
        return sum(float(n) * 60 ** i for i, n in enumerate(reversed(parts)))
    except ValueError:
        return 0.0


def collect(run, duration):
    mf = load_json(run / "frames_manifest.json")
    analysis = load_json(run / "tiered_analysis.json")
    verification = load_json(run / "verification.json")
    frames = mf.get("frames", [])
    if not frames:
        raise ValueError("No frames in " + str(run))
    area = [min(1.0, f["width"] * f["height"] /
                 max(1, f["source_width"] * f["source_height"])) for f in frames]
    times = sorted(set(float(f.get("actual_sec", f.get("requested_sec", 0))) for f in frames))
    edges = [0.0] + times + [max(float(duration), times[-1])]
    gaps = [b - a for a, b in zip(edges, edges[1:])]
    statuses = {}
    for row in verification.get("items", []):
        name = row.get("final_status", "UNKNOWN")
        statuses[name] = statuses.get(name, 0) + 1
    chapters = analysis.get("scene_analyses", [])
    return {
        "run_id": run.name,
        "frame_count": len(frames),
        "full_frame_count": sum(r >= 0.9999 for r in area),
        "screen_area_retained_pct": round(statistics.mean(area) * 100, 2),
        "max_gap_sec": round(max(gaps), 2),
        "median_gap_sec": round(statistics.median(gaps), 2),
        "roi_frame_count": mf.get("roi_frame_count", 0),
        "board_count": mf.get("board_count", len(mf.get("boards", []))),
        "logical_chapter_count": len(chapters),
        "source_scene_ids_provenanced": len({
            int(i) for ch in chapters for i in ch.get("source_scene_ids", [])
        }),
        "ui_settings_count": len(analysis.get("ui_settings_extracted", [])),
        "screen_text_items": len(analysis.get("prompts_extracted", [])),
        "ui_description_chars": sum(len(ch.get("visual_ui_text", "")) for ch in chapters),
        "verification_statuses": statuses,
        "_analysis": analysis,
    }


def visible_passage(analysis, sec, tolerance=20):
    out = []
    for chapter in analysis.get("scene_analyses", []):
        if float(chapter.get("start_sec", 0)) - tolerance <= sec <= float(chapter.get("end_sec", 0)) + tolerance:
            out.append(chapter.get("visual_ui_text", ""))
    for p in analysis.get("prompts_extracted", []):
        if abs(parse_sec(p.get("timestamp")) - sec) <= tolerance:
            out.append(p.get("verbatim_text", ""))
    for s in analysis.get("ui_settings_extracted", []):
        if abs(parse_sec(s.get("timestamp")) - sec) <= tolerance:
            out.extend((s.get("setting_name", ""), s.get("selected_value", "")))
    return "\n".join(str(x) for x in out)


def annotated_recall(run, checks):
    if not checks:
        return {"available": False, "reason": "Independent visual annotations not provided"}
    rows = []
    for check in checks:
        expected = normalize(check["expected_text"])
        if not expected:
            raise ValueError("Empty annotated expected_text")
        sec = float(check["timestamp_sec"])
        observed = normalize(visible_passage(
            run["_analysis"], sec, float(check.get("tolerance_sec", 20))
        ))
        rows.append({
            "id": check.get("id", str(sec)),
            "timestamp_sec": sec,
            "expected_text": check["expected_text"],
            "found": expected in observed,
        })
    hits = sum(row["found"] for row in rows)
    return {
        "available": True,
        "matched": hits,
        "missed": len(rows) - hits,
        "total": len(rows),
        "sampled_recall_pct": round(100 * hits / len(rows), 2),
        "sampled_omission_pct": round(100 * (len(rows) - hits) / len(rows), 2),
        "checks": rows,
        "note": "Strict string recall on human-verified image samples, not overall recognition accuracy",
    }


METRICS = [
    ("frame_count", "Selected keyframes"),
    ("full_frame_count", "Uncropped full-screen frames"),
    ("screen_area_retained_pct", "Mean screen area retained (%)"),
    ("roi_frame_count", "Focused ROI frames"),
    ("board_count", "Evidence boards"),
    ("max_gap_sec", "Maximum evidence gap (s)"),
    ("median_gap_sec", "Median evidence gap (s)"),
    ("logical_chapter_count", "Analysis chapters"),
    ("source_scene_ids_provenanced", "Source scenes linked to chapters"),
    ("screen_text_items", "Screen-text items"),
    ("ui_settings_count", "UI setting items"),
    ("ui_description_chars", "UI description characters"),
]


def compare_runs(baseline_dir, candidate_dir, duration=0, ground_truth=None):
    old = collect(Path(baseline_dir), duration)
    new = collect(Path(candidate_dir), duration)
    checks = load_json(Path(ground_truth)).get("checks", []) if ground_truth else []
    b = annotated_recall(old, checks)
    c = annotated_recall(new, checks)
    delta = {key: round(new[key] - old[key], 2) for key, _ in METRICS}
    old.pop("_analysis")
    new.pop("_analysis")
    return {
        "baseline": old,
        "candidate": new,
        "delta": delta,
        "sampled_visual_text": {"baseline": b, "candidate": c},
        "accuracy_limitation": (
            "No full-video recognition accuracy or omission rate can be inferred from "
            "model outputs. A manual image-grounded benchmark is required."
        ),
    }


def as_markdown(comparison):
    old, new = comparison["baseline"], comparison["candidate"]
    lines = [
        "# Multimodal V2 vs V3 evidence comparison", "",
        "Model-generated text is not independent truth. An increase in output text does not prove accuracy.",
        "", "| Metric | Baseline | Candidate | Delta |", "|---|---:|---:|---:|",
    ]
    for key, name in METRICS:
        lines.append(f"| {name} | {old[key]} | {new[key]} | {comparison['delta'][key]:+g} |")
    lines += [
        "", "## Verification provenance", "",
        "Baseline: " + str(old["verification_statuses"]),
        "Candidate: " + str(new["verification_statuses"]),
        "", "## Manual image-grounded screen-text sampling", "",
    ]
    scores = comparison["sampled_visual_text"]
    if scores["candidate"]["available"]:
        lines.extend([
            f"Baseline sampled recall: {scores['baseline']['sampled_recall_pct']}%",
            f"Candidate sampled recall: {scores['candidate']['sampled_recall_pct']}%",
            f"Baseline sampled omission: {scores['baseline']['sampled_omission_pct']}%",
            f"Candidate sampled omission: {scores['candidate']['sampled_omission_pct']}%",
            "",
            "| Timestamp | Expected image text | Baseline | Candidate |",
            "|---:|---|---|---|",
        ])
        for a, b in zip(scores["baseline"]["checks"], scores["candidate"]["checks"]):
            txt = str(a["expected_text"]).replace("|", "\\|")
            lines.append(
                f"| {a['timestamp_sec']:.1f} | {txt} | "
                f"{'found' if a['found'] else 'missing'} | "
                f"{'found' if b['found'] else 'missing'} |"
            )
    else:
        lines.append("NOT MEASURED: Independent image-grounded labels are missing.")
    lines += ["", comparison["accuracy_limitation"], ""]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("."))
    p.add_argument("--video-id", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--ground-truth", type=Path)
    p.add_argument("--output-dir", type=Path, default=Path("audit_upload"))
    a = p.parse_args()
    video_dir = a.root / "data" / "videos" / a.video_id
    meta = video_dir / "media_metadata.json"
    duration = float(load_json(meta).get("duration_sec", 0)) if meta.is_file() else 0.0
    result = compare_runs(
        video_dir / "runs" / a.baseline,
        video_dir / "runs" / a.candidate,
        duration=duration,
        ground_truth=a.ground_truth,
    )
    a.output_dir.mkdir(parents=True, exist_ok=True)
    dest_md = a.output_dir / "NotebookLM_Argon_V2_vs_V3_Comparison.md"
    dest_json = a.output_dir / "NotebookLM_Argon_V2_vs_V3_Comparison.json"
    dest_md.write_text(as_markdown(result), encoding="utf-8")
    dest_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[OK]", dest_md.resolve())
    print("[OK]", dest_json.resolve())


if __name__ == "__main__":
    main()
