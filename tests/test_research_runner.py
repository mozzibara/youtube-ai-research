"""Phase 2: integrated source→PyAV media→model mock→checkpoint→state tests."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

import av
from PIL import Image

from ytresearch.research_runner import ResearchRunInputError, run_research_video
from ytresearch.research_state import open_research_db


class ResearchOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "research").mkdir()
        (self.root / "research" / "index.json").write_text(json.dumps({
            "source_count": 77, "sources": [
                {"source_id": f"S{i:03d}", "source_ids": [f"S{i:03d}"],
                 "video_id": f"{i:011d}", "playlist_position": i}
                for i in range(1, 78)
            ],
        }), encoding="utf-8")
        self.video_id = "00000000001"
        self.video = self.root / "tiny.mp4"
        self.json3 = self.root / "tiny.json3"
        mux = av.open(str(self.video), "w")
        stream = mux.add_stream("mpeg4", rate=4)
        stream.width = 320
        stream.height = 180
        stream.pix_fmt = "yuv420p"
        for i in range(16):
            frame = av.VideoFrame.from_image(Image.new(
                "RGB", (320, 180), (25, 25, 25) if i < 8 else (220, 220, 220)
            ))
            for packet in stream.encode(frame):
                mux.mux(packet)
        for packet in stream.encode():
            mux.mux(packet)
        mux.close()
        self.json3.write_text(json.dumps({"events": [
            {"tStartMs": 0, "dDurationMs": 1800, "segs": [{"utf8": "메뉴를 클릭합니다"}]},
            {"tStartMs": 2000, "dDurationMs": 1800, "segs": [{"utf8": "두 번째 단계입니다"}]},
        ]}, ensure_ascii=False), encoding="utf-8")

    def test_live_agy_is_explicitly_required_and_no_files_are_mutated(self):
        with self.assertRaisesRegex(ResearchRunInputError, "LIVE_AGY_EXPLICIT"):
            run_research_video(self.root, self.video_id,
                               video_file=self.video, subtitle_json3=self.json3)
        self.assertFalse((self.root / "data" / "research_state.sqlite3").exists())
        self.assertFalse((self.root / "data" / "videos").exists())

    def test_unregistered_video_does_not_trigger_model(self):
        calls = []
        def mock(_):
            calls.append(True)
            return {}
        with self.assertRaisesRegex(ResearchRunInputError, "VIDEO_NOT_IN_REGISTERED"):
            run_research_video(self.root, "99999999999",
                               video_file=self.video, subtitle_json3=self.json3,
                               analyzer=mock, roi_mode="off")
        self.assertEqual(calls, [])

    def test_integrated_pyav_pipeline_registers_and_resumes_without_reinvoking(self):
        calls = []
        def mock(timeline):
            calls.append(len(timeline["units"]))
            return {"scene_analyses": [{
                "scene_id": row["scene_id"],
                "start_sec": row["start_sec"],
                "end_sec": row["end_sec"],
                "summary": "메뉴 선택",
                "visual_ui_text": "버튼",
                "verification_status": "VERIFIED",
                "confidence": 0.99,
            } for row in timeline["units"]],
                    "prompts_extracted": [], "ui_settings_extracted": []}

        args = dict(
            video_file=self.video, subtitle_json3=self.json3,
            primary_model="argon", run_id="ci_phase2_test", max_keyframes=4,
            roi_mode="off", analyzer=mock,
        )
        first = run_research_video(self.root, self.video_id, **args)
        self.assertEqual(first["source_ids"], ["S001"])
        self.assertEqual(first["run_status"], "COMPLETED")
        self.assertEqual(first["quality_status"], "PASS")
        self.assertTrue(Path(first["quality_gate_path"]).exists())
        self.assertTrue(Path(first["research_report_path"]).exists())
        self.assertEqual(first["research_report_review_state"], "REVIEW_REQUIRED")
        self.assertEqual(first["report_review_state"], "REVIEW_REQUIRED")
        self.assertTrue(Path(first["evidence_report_path"]).exists())
        self.assertEqual(len(calls), 1)
        again = run_research_video(self.root, self.video_id, **args)
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertEqual(len(calls), 1)  # Resume from checkpoint.
        with closing(open_research_db(self.root)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT review_state FROM reports").fetchone()[0],
                             "REVIEW_REQUIRED")

    def test_missing_inputs_rejected_before_state_registration(self):
        with self.assertRaisesRegex(ResearchRunInputError, "INPUT_MISSING_JSON3"):
            run_research_video(self.root, self.video_id,
                               video_file=self.video, subtitle_json3=self.root / "none.json3",
                               analyzer=lambda _: {})
        self.assertFalse((self.root / "data" / "research_state.sqlite3").exists())

    def test_live_and_mock_mode_cannot_be_combined(self):
        with self.assertRaisesRegex(ResearchRunInputError, "AMBIGUOUS"):
            run_research_video(self.root, self.video_id,
                               video_file=self.video, subtitle_json3=self.json3,
                               analyzer=lambda _: {}, use_live_agy=True)


if __name__ == "__main__":
    unittest.main()
