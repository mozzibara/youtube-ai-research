"""Offline regression tests for run-comparison metrics and strict image-truth recall."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.compare_multimodal_runs import compare_runs, as_markdown


class MultimodalComparisonTests(unittest.TestCase):
    def test_coverage_and_independent_sampled_text_recall(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "baseline"
            new = root / "candidate"
            old.mkdir()
            new.mkdir()
            for run, full in ((old, False), (new, True)):
                frames = [
                    {"requested_sec": 10, "actual_sec": 10,
                     "source_width": 1000, "source_height": 1000,
                     "width": 1000 if full else 720, "height": 1000 if full else 930},
                    {"requested_sec": 30, "actual_sec": 30,
                     "source_width": 1000, "source_height": 1000,
                     "width": 1000 if full else 720, "height": 1000 if full else 930},
                ]
                manifest = {"frames": frames, "frame_count": 2,
                            "roi_frame_count": 6 if full else 0,
                            "board_count": 4 if full else 1}
                analysis = {
                    "scene_analyses": [{
                        "scene_id": 1, "start_sec": 0, "end_sec": 50,
                        "source_scene_ids": [1, 2] if full else [],
                        "visual_ui_text": "NotebookLM 인포그래픽 맞춤설정" if full else "NotebookLM 화면",
                    }],
                    "prompts_extracted": [], "ui_settings_extracted": [],
                }
                verification = {
                    "items": [{"final_status": "SELF_CHECKED" if full else "VERIFIED"}]
                }
                for filename, obj in (
                    ("frames_manifest.json", manifest),
                    ("tiered_analysis.json", analysis),
                    ("verification.json", verification),
                ):
                    (run / filename).write_text(
                        json.dumps(obj, ensure_ascii=False), encoding="utf-8"
                    )
            truth = root / "truth.json"
            truth.write_text(json.dumps({"checks": [
                {"id": "ui01", "timestamp_sec": 20, "expected_text": "인포그래픽 맞춤설정"}
            ]}, ensure_ascii=False), encoding="utf-8")
            comparison = compare_runs(old, new, duration=50, ground_truth=truth)
            self.assertEqual(comparison["baseline"]["screen_area_retained_pct"], 66.96)
            self.assertEqual(comparison["candidate"]["screen_area_retained_pct"], 100)
            self.assertEqual(comparison["delta"]["full_frame_count"], 2)
            self.assertEqual(comparison["candidate"]["roi_frame_count"], 6)
            self.assertEqual(comparison["sampled_visual_text"]["baseline"]["sampled_recall_pct"], 0)
            self.assertEqual(comparison["sampled_visual_text"]["candidate"]["sampled_recall_pct"], 100)
            md = as_markdown(comparison)
            self.assertIn("Baseline sampled recall", md)
            self.assertIn("100", md)

    def test_without_ground_truth_does_not_invent_accuracy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("old", "new"):
                target = root / name
                target.mkdir()
                (target / "frames_manifest.json").write_text(json.dumps({"frames": [
                    {"actual_sec": 1, "width": 100, "height": 100,
                     "source_width": 100, "source_height": 100}
                ]}), encoding="utf-8")
                (target / "tiered_analysis.json").write_text(json.dumps({
                    "scene_analyses": [], "prompts_extracted": [],
                    "ui_settings_extracted": []
                }), encoding="utf-8")
                (target / "verification.json").write_text(json.dumps({
                    "items": []
                }), encoding="utf-8")
            comparison = compare_runs(root / "old", root / "new")
            self.assertFalse(comparison["sampled_visual_text"]["candidate"]["available"])
            self.assertIn("NOT MEASURED", as_markdown(comparison))


if __name__ == "__main__":
    unittest.main()
