"""Regression gates for all audited P0 and P1 multimodal defects."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from ytresearch.multimodal import (
    DEFAULT_CROP_BOX,
    assemble_final_markdown,
    compute_multimodal_run_id,
    save_verification_results,
    select_curated_ui_timestamps,
)
from ytresearch.multimodal_quality import (
    attach_chapter_provenance,
    build_provenance_verification_items,
    classify_evidence_items,
    deduplicate_visual_frames,
    resolve_chapter_source_units,
)


class MultimodalQualityRegressionTests(unittest.TestCase):
    def test_default_crop_is_full_screen_and_run_id_separates_roi_mode(self):
        self.assertEqual(DEFAULT_CROP_BOX, (0.0, 0.0, 1.0, 1.0))
        auto_id, auto_config = compute_multimodal_run_id(
            primary_model="gemini-4-argon-high", roi_mode="auto",
        )
        off_id, off_config = compute_multimodal_run_id(
            primary_model="gemini-4-argon-high", roi_mode="off",
        )
        self.assertNotEqual(auto_id, off_id)
        self.assertNotEqual(auto_config["config_hash"], off_config["config_hash"])
        self.assertEqual(auto_config["evidence_board_config"]["crop_box"], [0, 0, 1, 1])
        self.assertIn("right", auto_config["evidence_board_config"]["roi_boxes"])

    def test_chapter_id_cannot_alias_original_scene_id(self):
        timeline = {"units": [
            {"scene_id": 1, "start_sec": 0, "end_sec": 10,
             "ui_density_score": 0.1, "segment_ids": ["s000001"], "subtitles": [
                 {"segment_id": "s000001", "text": "incorrect source first"}], "keyframes": []},
            {"scene_id": 2, "start_sec": 10, "end_sec": 20,
             "ui_density_score": 0.2, "segment_ids": ["s000002"], "subtitles": [
                 {"segment_id": "s000002", "text": "second"}], "keyframes": []},
            {"scene_id": 3, "start_sec": 20, "end_sec": 30,
             "ui_density_score": 0.3, "segment_ids": ["s000003"], "subtitles": [
                 {"segment_id": "s000003", "text": "third"}], "keyframes": []},
        ]}
        logical = {"scene_id": 1, "start_sec": 10, "end_sec": 30, "summary": "chapter"}
        unit = resolve_chapter_source_units(timeline, logical)
        self.assertEqual(unit["chapter_id"], 1)
        self.assertEqual(unit["source_scene_ids"], [2, 3])
        self.assertNotIn("incorrect source first", unit["combined_transcript_text"])
        annotated = attach_chapter_provenance({"scene_analyses": [logical]}, timeline)
        self.assertEqual(annotated["scene_analyses"][0]["source_scene_ids"], [2, 3])
        self.assertEqual(annotated["scene_analyses"][0]["evidence_segment_ids"], ["s000002", "s000003"])
        self.assertEqual(annotated["analysis_id_type"], "LOGICAL_CHAPTER")

    def test_primary_argon_is_not_falsely_counted_as_independent_review(self):
        analysis = {"argon_invoked": False, "argon_verified_scene_ids": [], "scene_analyses": [
            {"scene_id": 1, "chapter_id": 1, "source_scene_ids": [2, 3],
             "start_sec": 10, "summary": "claim from one model",
             "visual_ui_text": "must not become ground truth",
             "verification_status": "VERIFIED"},
        ]}
        items = build_provenance_verification_items(
            analysis, primary_model="gemini-4-argon-high",
        )
        self.assertEqual(items[0]["final_status"], "SELF_CHECKED")
        self.assertEqual(items[0]["flash_verdict"], "NOT_CALLED")
        self.assertFalse(items[0]["independent_verifier_invoked"])
        self.assertEqual(items[0]["ground_truth"], None)
        self.assertTrue(items[0]["human_review_required"])

    def test_separate_model_review_is_not_ground_truth(self):
        analysis = {"argon_invoked": True, "argon_verified_scene_ids": [1], "scene_analyses": [
            {"scene_id": 1, "start_sec": 0, "summary": "model claim", "verification_status": "VERIFIED"},
            {"scene_id": 2, "start_sec": 10, "summary": "unreviewed", "verification_status": "VERIFIED"},
        ]}
        items = build_provenance_verification_items(
            analysis, primary_model="gemini-3.8-flash-medium",
        )
        self.assertEqual([i["final_status"] for i in items], ["MODEL_CROSS_CHECKED", "SELF_CHECKED"])
        self.assertTrue(items[0]["independent_verifier_invoked"])
        self.assertFalse(items[1]["independent_verifier_invoked"])
        self.assertIsNone(items[0]["ground_truth"])

    def test_evidence_types_and_truncation_are_explicit(self):
        result = classify_evidence_items({"prompts_extracted": [
            {"label": "사용자 입력 프롬프트", "verbatim_text": "create three topics"},
            {"label": "AI 응답 추천 질문", "verbatim_text": "topic 1..."},
            {"label": "UI 플레이스홀더", "verbatim_text": "힌트"},
            {"label": "슬라이드 생성 결과", "verbatim_text": "slide 1"},
            {"label": "불확실한 텍스트", "verbatim_text": "unclassified"},
        ]})
        items = result["prompts_extracted"]
        self.assertEqual([x["evidence_kind"] for x in items], [
            "USER_PROMPT", "AI_OUTPUT", "UI_PLACEHOLDER",
            "GENERATED_CONTENT", "SCREEN_TEXT_UNSPECIFIED",
        ])
        self.assertEqual(items[1]["transcription_completeness"], "TRUNCATED_VISIBLE")
        self.assertEqual(items[0]["transcription_completeness"], "UNCONFIRMED_COMPLETE")
        self.assertTrue(all(x["human_review_required"] for x in items))

    def test_keyframe_selection_balances_time_and_minimum_gap(self):
        scenes = [{
            "scene_id": i + 1, "start_sec": 10 * i,
            "end_sec": 10 * (i + 1), "keyframe_sec": 10 * i + 2,
            "mad_score": i % 5 + 5, "ui_density_score": 0.01 + 0.002 * (i % 7),
            "is_ui_dense": i % 3 == 0,
        } for i in range(40)]
        timestamps = select_curated_ui_timestamps(
            scenes, {"segments": []}, max_keyframes=12, min_gap_sec=4.5,
        )
        self.assertEqual(len(timestamps), 12)
        self.assertLess(timestamps[0], 60)
        self.assertGreater(timestamps[-1], 330)
        self.assertTrue(all(b - a >= 4.5 for a, b in zip(timestamps, timestamps[1:])))

    def test_pixel_duplicate_rejection_preserves_temporally_distant_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / f"frame_{i}.png" for i in range(3)]
            for p in paths:
                Image.new("RGB", (120, 80), (130, 140, 150)).save(p)
            frames = [
                {"frame_id": "f1", "path": str(paths[0]), "actual_sec": 10},
                {"frame_id": "f2", "path": str(paths[1]), "actual_sec": 15},
                {"frame_id": "f3", "path": str(paths[2]), "actual_sec": 180},
            ]
            kept, removed = deduplicate_visual_frames(frames)
            self.assertEqual([f["frame_id"] for f in kept], ["f1", "f3"])
            self.assertEqual(removed[0]["duplicate_of"], "f1")

    def test_offline_end_to_end_creates_full_frames_and_roi_boards(self):
        """Real PyAV/Pillow extraction smoke test; no AGY call is made."""
        import av
        import json
        from ytresearch.multimodal import run_resumable_multimodal_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "tiny_video.mp4"
            container = av.open(str(video), mode="w")
            stream = container.add_stream("mpeg4", rate=4)
            stream.width, stream.height = 320, 180
            stream.pix_fmt = "yuv420p"
            for i in range(16):
                color = (30, 30, 30) if i < 8 else (235, 235, 235)
                frame = av.VideoFrame.from_image(Image.new("RGB", (320, 180), color))
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
            container.close()
            subs = root / "subs.json3"
            subs.write_text(json.dumps({"events": [
                {"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "버튼 클릭"}]},
                {"tStartMs": 2200, "dDurationMs": 1500, "segs": [{"utf8": "오른쪽 설정 메뉴"}]},
            ]}), encoding="utf-8")

            def offline_analyzer(timeline):
                return {
                    "scene_analyses": [{
                        "scene_id": u["scene_id"], "start_sec": u["start_sec"],
                        "end_sec": u["end_sec"], "summary": "mock",
                        "visual_ui_text": "mock ui",
                        "verification_status": "VERIFIED", "confidence": 0.9,
                    } for u in timeline["units"]],
                    "prompts_extracted": [
                        {"label": "AI 응답", "timestamp": "00:02",
                         "verbatim_text": "something..."}],
                    "ui_settings_extracted": [],
                }

            result = run_resumable_multimodal_pipeline(
                root, "Tst00000075", video_path=video, subtitle_json3_path=subs,
                flash_analyzer=offline_analyzer,
                primary_model="argon", max_keyframes=4, roi_mode="auto",
            )
            self.assertEqual(result["checkpoint"]["status"], "COMPLETED")
            run_dir = Path(result["checkpoint_path"]).parent
            manifest = json.loads((run_dir / "frames_manifest.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(manifest["frame_count"], 1)
            self.assertGreaterEqual(manifest["roi_frame_count"], 3)
            self.assertGreater(manifest["board_count"], 1)
            self.assertTrue(all(f["width"] == f["source_width"]
                                and f["height"] == f["source_height"]
                                for f in manifest["frames"]))
            self.assertTrue(any(b["board_id"].startswith("roi_board")
                                for b in manifest["boards"]))
            self.assertTrue((root / "data/videos/Tst00000075/scenes_full_v3.json").exists())
            self.assertFalse((root / "data/videos/Tst00000075/scenes.json").exists())
            verification = json.loads(Path(result["verification_path"]).read_text(encoding="utf-8"))
            self.assertEqual(verification["status_counts"]["VERIFIED"], 0)
            self.assertGreater(verification["status_counts"]["SELF_CHECKED"], 0)
            self.assertTrue(all(item["ground_truth"] is None for item in verification["items"]))
            report = Path(result["report_path"]).read_text(encoding="utf-8")
            self.assertIn("SELF_CHECKED", report)
            self.assertIn("AI_OUTPUT", report)

    def test_report_and_verification_labels_do_not_promise_independent_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = build_provenance_verification_items(
                {"argon_invoked": False, "scene_analyses": [
                    {"scene_id": 1, "chapter_id": 1, "source_scene_ids": [7, 8],
                     "start_sec": 0, "summary": "sample", "verification_status": "VERIFIED"}
                ]},
                primary_model="gemini-4-argon-high",
            )
            report = save_verification_results(Path(tmp), "Tst00000075", items,
                                               output_path=Path(tmp) / "verification.json")
            self.assertEqual(report["status_counts"]["SELF_CHECKED"], 1)
            self.assertEqual(report["status_counts"]["VERIFIED"], 0)
            self.assertIsNone(report["items"][0]["ground_truth"])
            self.assertEqual(report["items"][0]["source_scene_ids"], [7, 8])
            md = assemble_final_markdown(
                video_id="Tst00000075", title="test",
                url="https://youtube.com/watch?v=Tst00000075",
                media_metadata={}, parsed_subtitles={"segments": []},
                analysis_result={
                    "scene_analyses": items and [{
                        "scene_id": 1, "chapter_id": 1, "source_scene_ids": [7, 8],
                        "start_sec": 0, "end_sec": 20, "summary": "sample"
                    }],
                    "prompts_extracted": [{
                        "label": "AI 응답", "verbatim_text": "excerpt...",
                        "evidence_kind": "AI_OUTPUT",
                        "transcription_completeness": "TRUNCATED_VISIBLE"
                    }],
                },
                verification_report=report,
            )
            self.assertIn("Chapter 1", md)
            self.assertIn("SELF_CHECKED", md)
            self.assertIn("원문 완전성 검증 대기", md)
            self.assertIn("TRUNCATED_VISIBLE", md)
            self.assertIn("7, 8", md)
            self.assertNotIn("Scene 1", md)


if __name__ == "__main__":
    unittest.main()
