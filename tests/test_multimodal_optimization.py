"""Unit and integration tests for multimodal optimization pipeline (Flash default + conditional Argon)."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import av
from PIL import Image, ImageDraw

from ytresearch import __main__ as cli
from ytresearch.batch import sync_sources_csv
from ytresearch.collector import ingest_json3
from ytresearch.multimodal import (
    MultimodalCheckpointManager,
    SceneInterval,
    _build_logical_chapters,
    align_multimodal_timeline,
    assemble_final_markdown,
    build_visual_evidence_boards,
    cache_raw_media_bundle,
    calculate_token_cost,
    classify_run_independence,
    compute_adaptive_extraction_budget,
    compute_multimodal_run_id,
    detect_scene_transitions,
    detect_uncertain_intervals,
    extract_highres_ui_frames,
    load_media_cache,
    load_pricing_config,
    load_verification_results,
    parse_agy_session_transcript_usage,
    parse_agy_stream_json_usage,
    parse_json3_subtitles,
    probe_media_metadata,
    resolve_primary_model_choice,
    run_resumable_multimodal_pipeline,
    run_tiered_multimodal_analysis,
    save_run_token_usage,
    save_verification_results,
    select_adaptive_model_policy,
    select_curated_ui_timestamps,
    separate_workflow_costs,
    should_escalate_to_argon,
    verify_child_runtime_model,
)


def _create_synthetic_video(path: Path, *, width: int = 640, height: int = 360, fps: int = 10, duration_sec: int = 6) -> Path:
    """Create a small synthetic MP4 video with two distinct visual scenes (solid dark -> dense UI grid)."""
    container = av.open(str(path), mode="w")
    stream = container.add_stream("mpeg4", rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"

    total_frames = fps * duration_sec
    for idx in range(total_frames):
        sec = idx / float(fps)
        if sec < 3.0:
            # Scene 1: Low-density dark frame
            img = Image.new("RGB", (width, height), color=(20, 20, 25))
        else:
            # Scene 2: High-contrast UI pattern (simulated text/table bars)
            img = Image.new("RGB", (width, height), color=(245, 245, 250))
            draw = ImageDraw.Draw(img)
            for x in range(20, int(width * 0.7), 8):
                for y in range(20, height - 20, 12):
                    draw.rectangle([x, y, x + 3, y + 6], fill=(15, 15, 20))
        frame = av.VideoFrame.from_image(img)
        for packet in stream.encode(frame):
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


class MultimodalOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config_dir = self.root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agents.json").write_text(
            json.dumps({
                "extractor": {"model": "gemini-3.8-flash-medium", "effort": "medium"},
                "analyzer": {"model": "claude-sonnet-5-5-high", "effort": "high"},
                "auditor": {"model": "gemini-4-argon-high", "effort": "high"},
            }),
            encoding="utf-8",
        )
        self.video_id = "Tst00000075"

    def tearDown(self):
        self.tmp.cleanup()

    def test_media_cache_bundle_tracks_sha256_and_hits(self):
        vid_file = self.root / "sample.mp4"
        sub_file = self.root / "sample.json3"
        vid_file.write_bytes(b"fake-video-payload-123")
        sub_file.write_text('{"events":[]}', encoding="utf-8")

        first = cache_raw_media_bundle(
            self.root,
            self.video_id,
            video_path=vid_file,
            subtitle_path=sub_file,
            copy_files=True,
        )
        self.assertFalse(first["assets"]["video"]["cache_hit"])
        self.assertFalse(first["assets"]["subtitle_json3"]["cache_hit"])

        second = cache_raw_media_bundle(
            self.root,
            self.video_id,
            video_path=vid_file,
            subtitle_path=sub_file,
            copy_files=True,
        )
        self.assertTrue(second["assets"]["video"]["cache_hit"])
        self.assertTrue(second["assets"]["subtitle_json3"]["cache_hit"])
        loaded = load_media_cache(self.root, self.video_id)
        self.assertEqual(loaded["assets"]["video"]["sha256"], first["assets"]["video"]["sha256"])

    def test_code_based_json3_parser_preserves_ms_and_filters_empty_events(self):
        sample_json3 = {
            "events": [
                {"tStartMs": 0, "dDurationMs": 500},
                {"tStartMs": 760, "dDurationMs": 4199, "segs": [{"utf8": "자 여러분 "}, {"utf8": "이제 PPT"}]},
                {"tStartMs": 2920, "dDurationMs": 2039, "segs": [{"utf8": "\n"}]},
                {"tStartMs": 382919, "dDurationMs": 3000, "segs": [{"utf8": "구독과 좋아요"}]},
            ]
        }
        parsed = parse_json3_subtitles(sample_json3, video_id=self.video_id, language="ko")
        self.assertEqual(parsed["raw_event_count"], 4)
        self.assertEqual(parsed["skipped_empty_event_count"], 2)
        self.assertEqual(parsed["valid_segment_count"], 2)
        self.assertEqual(parsed["time_range_ms"]["first_start_ms"], 760)
        self.assertEqual(parsed["time_range_ms"]["last_start_ms"], 382919)
        self.assertEqual(parsed["time_range_ms"]["last_end_ms"], 385919)
        self.assertEqual(parsed["segments"][0]["id"], "s000001")
        self.assertEqual(parsed["segments"][0]["text"], "자 여러분 이제 PPT")
        self.assertEqual(parsed["segments"][1]["id"], "s000002")
        self.assertEqual(parsed["segments"][1]["timestamp"], "06:22")

    def test_ingest_json3_and_cli_import_json3(self):
        sources_csv = self.root / "sources.csv"
        sources_csv.write_text(
            f"source_id,url\nS004,https://www.youtube.com/watch?v={self.video_id}\n",
            encoding="utf-8",
        )
        sync_sources_csv(self.root, sources_csv, expected=1)

        json3_file = self.root / "video.ko-orig.json3"
        json3_file.write_text(
            json.dumps({
                "events": [
                    {"tStartMs": 760, "dDurationMs": 2000, "segs": [{"utf8": "첫 번째 자막"}]},
                    {"tStartMs": 3000, "dDurationMs": 1500, "segs": [{"utf8": "두 번째 자막"}]},
                ]
            }),
            encoding="utf-8",
        )
        argv = [
            "ytresearch", "--root", str(self.root), "import-json3",
            "--video-id", self.video_id,
            "--file", str(json3_file),
            "--language", "ko",
            "--expected", "1",
        ]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main()
        self.assertEqual(rc, 0)
        self.assertIn("Imported JSON3 Tst00000075: 2 segments", out.getvalue())

    def test_probe_detect_scenes_extract_frames_and_align_timeline(self):
        vid_path = _create_synthetic_video(self.root / "synth.mp4", width=640, height=360, fps=10, duration_sec=6)
        meta = probe_media_metadata(vid_path)
        self.assertEqual(meta["streams"][0]["width"], 640)
        self.assertEqual(meta["streams"][0]["height"], 360)
        self.assertAlmostEqual(meta["duration_sec"], 6.0, delta=0.5)

        scenes = detect_scene_transitions(
            vid_path,
            sample_interval_sec=1.0,
            mad_threshold=10.0,
            min_scene_duration_sec=2.0,
            ui_density_threshold=0.02,
        )
        self.assertGreaterEqual(len(scenes), 2)
        self.assertFalse(scenes[0].is_ui_dense)
        self.assertTrue(scenes[-1].is_ui_dense)

        frames = extract_highres_ui_frames(
            vid_path,
            self.root / "frames",
            [scenes[0].keyframe_sec, scenes[-1].keyframe_sec],
            crop_box=(0.0, 0.0, 0.75, 1.0),
        )
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["width"], 480)
        self.assertTrue(Path(frames[0]["path"]).exists())

        parsed_subs = parse_json3_subtitles(
            {
                "events": [
                    {"tStartMs": 500, "dDurationMs": 1500, "segs": [{"utf8": "도입부 화면 설명"}]},
                    {"tStartMs": 3500, "dDurationMs": 2000, "segs": [{"utf8": "UI 패널 설정 변경"}]},
                ]
            },
            video_id=self.video_id,
        )
        aligned = align_multimodal_timeline(scenes, parsed_subs, extracted_frames=frames)
        self.assertEqual(aligned["total_scenes"], len(scenes))
        self.assertEqual(aligned["total_subtitles"], 2)
        self.assertIn("s000001", aligned["units"][0]["segment_ids"])
        self.assertIn("s000002", aligned["units"][-1]["segment_ids"])

    def test_tiered_analysis_selective_reanalysis_and_conditional_argon(self):
        aligned = {
            "video_id": self.video_id,
            "units": [
                {
                    "scene_id": 1,
                    "start_sec": 0.0,
                    "end_sec": 10.0,
                    "keyframe_sec": 2.0,
                    "ui_density_score": 0.02,
                    "is_ui_dense": False,
                    "keyframes": [],
                    "combined_transcript_text": "안녕하세요 알렉스입니다",
                },
                {
                    "scene_id": 2,
                    "start_sec": 280.0,
                    "end_sec": 310.0,
                    "keyframe_sec": 286.5,
                    "ui_density_score": 0.08,
                    "is_ui_dense": True,
                    "keyframes": [],
                    "combined_transcript_text": "챗GPT 거를 쓰거든요 달리 오픈AI 거를 쓰기 때문에",
                },
            ],
        }

        def fake_flash(tl):
            return {
                "scene_analyses": [
                    {
                        "scene_id": 1,
                        "start_sec": 0.0,
                        "end_sec": 10.0,
                        "summary": "인사 및 도입",
                        "visual_ui_text": "",
                        "confidence": 0.96,
                        "verification_status": "VERIFIED",
                    },
                    {
                        "scene_id": 2,
                        "start_sec": 280.0,
                        "end_sec": 310.0,
                        "summary": "DALL-E 3 이미지 생성",
                        "visual_ui_text": "DALL-E 3 프롬프트 입력",
                        "confidence": 0.65,
                        "verification_status": "UNVERIFIED",
                    },
                ]
            }

        def fake_selective_reanalyzer(uncertain, _tl):
            # Suppose selective re-analysis still leaves a conflict requiring Argon
            return {
                "scene_analyses": [
                    {
                        "scene_id": 2,
                        "start_sec": 280.0,
                        "end_sec": 310.0,
                        "summary": "AI 이미지 프롬프트 일부 불분명",
                        "visual_ui_text": "CONFLICT",
                        "confidence": 0.70,
                        "verification_status": "CONFLICT",
                    }
                ]
            }

        def fake_argon(uncertain, _tl):
            self.assertEqual(len(uncertain), 1)
            self.assertEqual(uncertain[0]["scene_id"], 2)
            return {
                "scene_analyses": [
                    {
                        "scene_id": 2,
                        "start_sec": 280.0,
                        "end_sec": 310.0,
                        "summary": "미디어 -> AI 이미지 선택 및 자동 생성 프롬프트 실행",
                        "visual_ui_text": "ChatGPT lecture for 25 million professionals, futuristic, digital illustration, vibrant colors, detailed, concept art, educational, dynamic composition",
                        "confidence": 0.99,
                        "verification_status": "VERIFIED",
                        "verified_by": "gemini-4-argon-high",
                    }
                ]
            }

        result = run_tiered_multimodal_analysis(
            aligned,
            flash_analyzer=fake_flash,
            selective_highres_reanalyzer=fake_selective_reanalyzer,
            argon_verifier=fake_argon,
        )
        self.assertTrue(result["argon_invoked"])
        self.assertEqual(result["selectively_reanalyzed_scene_ids"], [2])
        self.assertEqual(result["argon_verified_scene_ids"], [2])
        self.assertIn("ChatGPT lecture for 25 million professionals", result["scene_analyses"][1]["visual_ui_text"])

    def test_resumable_pipeline_skips_completed_stages_and_assembles_markdown(self):
        vid_path = _create_synthetic_video(self.root / "video.mp4", width=320, height=180, fps=5, duration_sec=4)
        sub_path = self.root / "subs.json3"
        sub_path.write_text(
            json.dumps({
                "events": [
                    {"tStartMs": 500, "dDurationMs": 1000, "segs": [{"utf8": "첫 장면 발화"}]},
                    {"tStartMs": 2500, "dDurationMs": 1000, "segs": [{"utf8": "두 번째 장면 발화"}]},
                ]
            }),
            encoding="utf-8",
        )

        call_counter = {"flash": 0}

        def counting_flash(tl):
            call_counter["flash"] += 1
            return {
                "scene_analyses": [
                    {
                        "scene_id": u["scene_id"],
                        "start_sec": u["start_sec"],
                        "end_sec": u["end_sec"],
                        "summary": u["combined_transcript_text"],
                        "spoken_summary": u["combined_transcript_text"],
                        "visual_ui_text": "생성 | 요약",
                        "confidence": 0.95,
                        "verification_status": "VERIFIED",
                    }
                    for u in tl["units"]
                ],
                "prompts_extracted": [
                    {"label": "1차 프롬프트", "timestamp": "00:35", "verbatim_text": "적절한 예시 5가지 알려줘봐"}
                ],
                "ui_settings_extracted": [
                    {"timestamp": "02:27", "setting_name": "카드당 텍스트 양", "selected_value": "요약", "status": "VERIFIED", "evidence_ref": "frame_001"}
                ],
            }

        first_run = run_resumable_multimodal_pipeline(
            self.root,
            self.video_id,
            video_path=vid_path,
            subtitle_json3_path=sub_path,
            title="테스트 영상",
            flash_analyzer=counting_flash,
        )
        self.assertEqual(call_counter["flash"], 1)
        self.assertEqual(first_run["checkpoint"]["status"], "COMPLETED")
        report_md = Path(first_run["report_path"]).read_text(encoding="utf-8")
        self.assertIn("적절한 예시 5가지 알려줘봐", report_md)
        self.assertIn("카드당 텍스트 양", report_md)
        self.assertIn("`s000001`", report_md)

        # Second run without force=True must resume from COMPLETED checkpoint without calling flash_analyzer again
        second_run = run_resumable_multimodal_pipeline(
            self.root,
            self.video_id,
            video_path=vid_path,
            subtitle_json3_path=sub_path,
            title="테스트 영상",
            flash_analyzer=counting_flash,
        )
        self.assertEqual(call_counter["flash"], 1)
        self.assertEqual(second_run["checkpoint"]["status"], "COMPLETED")

    def test_token_usage_accounting_splits_turns_and_prevents_cache_double_counting(self):
        log_path = self.root / "transcript.jsonl"
        sample_steps = [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-06T00:00:00Z", "content": "Turn 1"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-06T00:00:10Z",
                "input_tokens": 5000,
                "cache_read_tokens": 0,
                "output_tokens": 300,
                "tool_calls": [{"name": "read_file", "args": {"Path": "video.mp4"}}],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "created_at": "2026-11-06T00:00:12Z",
                "media": [{"mime_type": "video/mp4", "uri": "file:///video.mp4"}],
                "content": "Loaded video",
            },
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-06T00:00:30Z",
                "input_tokens": 2000,
                "cache_read_tokens": 5000,
                "output_tokens": 1200,
                "tool_calls": [],
            },
            {"step_index": 4, "type": "USER_INPUT", "created_at": "2026-11-06T00:10:00Z", "content": "Turn 2 Audit"},
            {
                "step_index": 5,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-06T00:10:20Z",
                "input_tokens": 1500,
                "cache_read_tokens": 8200,
                "output_tokens": 800,
                "tool_calls": [],
            },
        ]
        log_path.write_text("\n".join(json.dumps(s) for s in sample_steps), encoding="utf-8")

        parsed = parse_agy_session_transcript_usage(log_path, split_by_user_turns=True)
        self.assertEqual(len(parsed["turns"]), 2)
        t1, t2 = parsed["turns"][0], parsed["turns"][1]
        self.assertEqual(t1["uncached_input_tokens"], 7000)
        self.assertEqual(t1["cache_read_tokens"], 5000)
        self.assertEqual(t1["output_tokens"], 1500)
        self.assertEqual(t1["total_tokens_uncached_plus_output"], 8500)
        self.assertEqual(t1["thinking_tokens"], "UNKNOWN")
        self.assertEqual(t1["media_input_count"], 1)

        self.assertEqual(t2["uncached_input_tokens"], 1500)
        self.assertEqual(t2["output_tokens"], 800)

        stream_raw = "\n".join([
            json.dumps({"event": "init", "init": {"model": "gemini-3.8-flash-medium"}}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "step_type": "agent_response",
                    "usage": {"input_tokens": 2347, "output_tokens": 1134, "thinking_tokens": 400, "cache_read_tokens": 18412, "total_tokens": 3481},
                },
            }),
            json.dumps({"event": "result", "result": {"duration_seconds": 14.2}}),
        ])
        s_usage = parse_agy_stream_json_usage(stream_raw)
        self.assertEqual(s_usage["uncached_input_tokens"], 2347)
        self.assertEqual(s_usage["cache_read_tokens"], 18412)
        self.assertEqual(s_usage["output_tokens"], 1134)
        self.assertEqual(s_usage["thinking_tokens"], 400)
        self.assertEqual(s_usage["visible_output_tokens"], 734)
        self.assertEqual(s_usage["total_tokens_excluding_cache"], 3481)

        ledger = save_run_token_usage(self.root, self.video_id, s_usage, run_label="unit_test")
        self.assertTrue(ledger.exists())

    def test_visual_evidence_boards_logical_chapters_and_multimodal_run_cli(self):
        vid_path = _create_synthetic_video(self.root / "board_video.mp4", width=640, height=360, fps=5, duration_sec=6)
        sub_path = self.root / "board_subs.json3"
        sub_path.write_text(
            json.dumps({
                "events": [
                    {"tStartMs": 500, "dDurationMs": 1500, "segs": [{"utf8": "프롬프트 예시 입력"}]},
                    {"tStartMs": 3200, "dDurationMs": 2000, "segs": [{"utf8": "테마 선택 및 생성 버튼 클릭"}]},
                ]
            }),
            encoding="utf-8",
        )
        parsed_subs = parse_json3_subtitles(sub_path, video_id=self.video_id)
        scenes = detect_scene_transitions(
            vid_path,
            sample_interval_sec=1.0,
            mad_threshold=5.0,
            min_scene_duration_sec=1.5,
            ui_density_threshold=0.01,
        )
        curated_ts = select_curated_ui_timestamps(scenes, parsed_subs, max_keyframes=4, min_gap_sec=0.5)
        self.assertGreaterEqual(len(curated_ts), 1)

        extracted = extract_highres_ui_frames(vid_path, self.root / "board_crops", [0.5, 3.5])
        boards = build_visual_evidence_boards(
            extracted,
            self.root / "boards",
            panels_per_board=2,
            max_panel_width=800,
        )
        self.assertEqual(len(boards), 1)
        self.assertTrue(Path(boards[0]["path"]).exists())
        self.assertEqual(boards[0]["panel_count"], 2)

        aligned = align_multimodal_timeline(scenes, parsed_subs)
        chapters = _build_logical_chapters(aligned, target_chapters=2)
        self.assertLessEqual(len(chapters), 2)

        meta = probe_media_metadata(vid_path)
        budget = compute_adaptive_extraction_budget(meta, scenes, parsed_subs)
        self.assertGreaterEqual(budget["target_boards"], 3)
        self.assertGreaterEqual(budget["target_chapters"], 4)
        policy = select_adaptive_model_policy(budget)
        self.assertIn(policy["primary_model"], ("gemini-3.8-flash-medium", "gemini-4-argon-high"))
        self.assertIn("markdown_assembled", policy["python_only_stages"])

        argv = [
            "ytresearch", "--root", str(self.root), "multimodal-run",
            "--video-id", self.video_id,
            "--video-file", str(vid_path),
            "--subtitle-file", str(sub_path),
            "--title", "CLI 테스트 영상",
        ]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main()
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["checkpoint"]["status"], "COMPLETED")
        self.assertTrue(Path(payload["report_path"]).exists())

    def test_official_pricing_cost_separation_init_model_and_independence(self):
        pricing = load_pricing_config()
        self.assertEqual(pricing["effective_date"], "2026-11-06")
        self.assertEqual(pricing["source_url"], "https://cloud.google.com/model-garden/pricing")
        self.assertEqual(pricing["models"]["gemini-3.8-flash-medium"]["input_per_1m_usd"], 0.75)
        self.assertEqual(pricing["models"]["gemini-3.8-flash-medium"]["output_per_1m_usd"], 3.75)
        self.assertEqual(pricing["models"]["gemini-3.8-flash-medium"]["cached_input_per_1m_usd"], 0.075)
        self.assertEqual(pricing["models"]["gemini-4-argon-high"]["input_per_1m_usd"], 2.00)
        self.assertEqual(pricing["models"]["gemini-4-argon-high"]["output_per_1m_usd"], 10.00)
        self.assertEqual(pricing["models"]["gemini-4-argon-high"]["cached_input_per_1m_usd"], 0.10)

        # Raw token usage remains untouched when calculating official cost
        c3_usage = {
            "model": "gemini-3.8-flash-medium",
            "uncached_input_tokens": 28428,
            "cache_read_tokens": 0,
            "output_tokens": 15342,
        }
        c3_snapshot = dict(c3_usage)
        c3_cost = calculate_token_cost(c3_usage, pricing)
        self.assertEqual(c3_usage, c3_snapshot)
        self.assertAlmostEqual(c3_cost["input_cost_usd"], 0.021321, places=6)
        self.assertAlmostEqual(c3_cost["output_cost_usd"], 0.057533, places=6)
        self.assertAlmostEqual(c3_cost["total_cost_usd"], 0.078853, places=5)
        self.assertEqual(c3_cost["agy_quota_deduction"], "UNKNOWN")

        e_usage = {
            "model": "gemini-4-argon-high",
            "uncached_input_tokens": 19170,
            "cache_read_tokens": 8879,
            "output_tokens": 16399,
        }
        e_cost = calculate_token_cost(e_usage, pricing)
        self.assertAlmostEqual(e_cost["input_cost_usd"], 0.038340, places=6)
        self.assertAlmostEqual(e_cost["cached_input_cost_usd"], 0.000888, places=6)
        self.assertAlmostEqual(e_cost["output_cost_usd"], 0.163990, places=6)
        self.assertAlmostEqual(e_cost["total_cost_usd"], 0.203218, places=6)
        self.assertEqual(e_cost["agy_quota_deduction"], "UNKNOWN")

        d_usage = {
            "model": "gemini-4-argon-high",
            "uncached_input_tokens": 9164,
            "cache_read_tokens": 3989,
            "output_tokens": 4832,
        }
        main_rd_usage = {
            "model": "gemini-4-argon-high",
            "uncached_input_tokens": 1410970,
            "cache_read_tokens": 24365256,
            "output_tokens": 331133,
        }
        separated = separate_workflow_costs(
            c3_usage,
            verifier_usage=d_usage,
            main_session_usage=main_rd_usage,
            pricing_config=pricing,
        )
        self.assertAlmostEqual(separated["batch_operational_child_cost_usd"], 0.078853, places=5)
        self.assertAlmostEqual(separated["batch_operational_verifier_cost_usd"], 0.067047, places=6)
        self.assertAlmostEqual(separated["batch_operational_total_cost_usd"], 0.145900, places=5)
        self.assertAlmostEqual(separated["rd_management_main_session_cost_usd"], 8.569796, places=6)
        self.assertFalse(separated["main_session_invoked_in_batch_mode"])
        self.assertEqual(separated["agy_quota_deduction"], "UNKNOWN")

        # Verify child runtime model via init.model and mismatch rejection
        stream_flash = json.dumps({"event": "init", "init": {"model": "gemini-3.8-flash-medium"}})
        v_ok = verify_child_runtime_model(stream_flash, "gemini-3.8-flash-medium", strict=True)
        self.assertTrue(v_ok["model_verified"])
        self.assertEqual(v_ok["init_model"], "gemini-3.8-flash-medium")
        with self.assertRaises(RuntimeError):
            verify_child_runtime_model(stream_flash, "gemini-4-argon-high", strict=True)

        # Independence classification: C2 (ARGON_ASSISTED), C3 (INDEPENDENT_FLASH), E (INDEPENDENT_ARGON)
        self.assertEqual(
            classify_run_independence(
                "gemini-3.8-flash-medium",
                prompt_text="04:46: ChatGPT lecture for 25 million professionals",
            ),
            "ARGON_ASSISTED",
        )
        self.assertEqual(
            classify_run_independence("gemini-3.8-flash-medium", prompt_text="No ground-truth hints"),
            "INDEPENDENT_FLASH",
        )
        self.assertEqual(
            classify_run_independence("gemini-4-argon-high", prompt_text="No ground-truth hints"),
            "INDEPENDENT_ARGON",
        )

    def test_new_video_stores_raw_media_in_media_dir_and_preserves_existing_cache(self):
        new_vid = "nEwViD12345"
        downloads = self.root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        src_video = _create_synthetic_video(downloads / "video.f398.mp4", width=320, height=180, fps=5, duration_sec=4)
        src_highres = _create_synthetic_video(downloads / "video.f401.mp4", width=640, height=360, fps=5, duration_sec=4)
        src_sub = downloads / "video.ko-orig.json3"
        src_sub.write_text(
            json.dumps({
                "events": [
                    {"tStartMs": 500, "dDurationMs": 1000, "segs": [{"utf8": "신규 영상 자막 1"}]},
                    {"tStartMs": 2200, "dDurationMs": 1000, "segs": [{"utf8": "신규 영상 자막 2"}]},
                ]
            }),
            encoding="utf-8",
        )

        summary = run_resumable_multimodal_pipeline(
            self.root,
            new_vid,
            video_path=src_video,
            highres_video_path=src_highres,
            subtitle_json3_path=src_sub,
            title="신규 영상 캐시 테스트",
        )
        self.assertEqual(summary["checkpoint"]["status"], "COMPLETED")

        expected_media_dir = (self.root / "data" / "videos" / new_vid / "media").resolve()
        cache_manifest = load_media_cache(self.root, new_vid)
        self.assertIsNotNone(cache_manifest)
        for kind, fname in (
            ("video", "video.f398.mp4"),
            ("highres_video", "video.f401.mp4"),
            ("subtitle_json3", "video.ko-orig.json3"),
        ):
            asset = cache_manifest["assets"][kind]
            cached_p = Path(asset["cached_path"]).resolve()
            self.assertEqual(cached_p, expected_media_dir / fname)
            self.assertTrue(cached_p.exists())

    def test_primary_model_cli_argon_flash_auto_and_init_model_cost(self):
        vid_path = _create_synthetic_video(self.root / "pm_video.mp4", width=320, height=180, fps=5, duration_sec=4)
        sub_path = self.root / "pm_subs.json3"
        sub_path.write_text(
            json.dumps({
                "events": [
                    {"tStartMs": 400, "dDurationMs": 1200, "segs": [{"utf8": "프롬프트 설정 시연"}]},
                    {"tStartMs": 2200, "dDurationMs": 1200, "segs": [{"utf8": "슬라이드 테마 선택"}]},
                ]
            }),
            encoding="utf-8",
        )

        # Verify resolve_primary_model_choice and select_adaptive_model_policy for argon, flash, auto
        self.assertEqual(resolve_primary_model_choice("argon"), ("gemini-4-argon-high", "argon"))
        self.assertEqual(resolve_primary_model_choice("flash"), ("gemini-3.8-flash-medium", "flash"))
        self.assertEqual(resolve_primary_model_choice("auto"), (None, "auto"))
        with self.assertRaises(ValueError):
            resolve_primary_model_choice("invalid-model")

        sample_budget = {"complexity_tier": "LOW_TALKING_HEAD", "target_boards": 4}
        pol_argon = select_adaptive_model_policy(sample_budget, prefer_model="argon")
        self.assertEqual(pol_argon["primary_model"], "gemini-4-argon-high")
        self.assertEqual(pol_argon["primary_effort"], "high")
        self.assertEqual(pol_argon["independence_mode"], "INDEPENDENT_ARGON")

        pol_flash = select_adaptive_model_policy({"complexity_tier": "HIGH_DENSE_CODE_OR_FINE_UI", "target_boards": 8}, prefer_model="flash")
        self.assertEqual(pol_flash["primary_model"], "gemini-3.8-flash-medium")
        self.assertEqual(pol_flash["primary_effort"], "medium")
        self.assertEqual(pol_flash["independence_mode"], "INDEPENDENT_FLASH")

        pol_auto = select_adaptive_model_policy(sample_budget, prefer_model="auto")
        self.assertEqual(pol_auto["primary_model"], "gemini-3.8-flash-medium")

        # Mock run_agy_flash_multimodal_analyzer to verify CLI --primary-model argon and --primary-model flash
        recorded_models = []

        def fake_agy_analyzer(root, video_id, tl, *, board_paths, parsed_subtitles=None, evidence_manifest=None, model="gemini-3.8-flash-medium", effort="medium", agent_name="yt-research-multimodal", run_label="default"):
            recorded_models.append((model, effort, run_label))
            out_doc = {
                "scene_analyses": [
                    {
                        "scene_id": 1,
                        "start_sec": 0.0,
                        "end_sec": 4.0,
                        "summary": f"Analyzed by {model}",
                        "visual_ui_text": "[화면] UI",
                        "spoken_summary": "[자막] 발화",
                        "confidence": 0.99,
                        "verification_status": "VERIFIED",
                        "verified_by": model,
                        "evidence_ref": "board_01.png (PANEL #01)",
                    }
                ],
                "prompts_extracted": [],
                "ui_settings_extracted": [],
            }
            usage = {
                "model": model,
                "init_model": model,
                "expected_model": model,
                "model_verified": True,
                "independence_mode": classify_run_independence(model),
                "uncached_input_tokens": 19170 if "argon" in model else 28428,
                "cache_read_tokens": 8879 if "argon" in model else 0,
                "output_tokens": 16399 if "argon" in model else 15342,
                "agy_quota_deduction": "UNKNOWN",
            }
            return out_doc, usage

        with patch("ytresearch.multimodal.run_agy_flash_multimodal_analyzer", side_effect=fake_agy_analyzer):
            argv_argon = [
                "ytresearch", "--root", str(self.root), "multimodal-run",
                "--video-id", "vIdArGoN001",
                "--video-file", str(vid_path),
                "--subtitle-file", str(sub_path),
                "--primary-model", "argon",
            ]
            with patch.object(sys, "argv", argv_argon), contextlib.redirect_stdout(io.StringIO()) as out_a:
                rc_a = cli.main()
            self.assertEqual(rc_a, 0)
            res_argon = json.loads(out_a.getvalue())
            self.assertEqual(res_argon["primary_model"], "gemini-4-argon-high")
            self.assertEqual(res_argon["primary_model_mode"], "argon")
            self.assertEqual(recorded_models[-1][0], "gemini-4-argon-high")
            self.assertEqual(recorded_models[-1][1], "high")
            argon_analysis = json.loads(Path(res_argon["analysis_path"]).read_text(encoding="utf-8"))
            self.assertEqual(argon_analysis["default_model"], "gemini-4-argon-high")
            self.assertAlmostEqual(argon_analysis["cost_summary"]["batch_operational_total_cost_usd"], 0.203218, places=5)

            argv_flash = [
                "ytresearch", "--root", str(self.root), "multimodal-run",
                "--video-id", "vIdArGoN001",
                "--video-file", str(vid_path),
                "--subtitle-file", str(sub_path),
                "--primary-model", "flash",
            ]
            with patch.object(sys, "argv", argv_flash), contextlib.redirect_stdout(io.StringIO()) as out_f:
                rc_f = cli.main()
            self.assertEqual(rc_f, 0)
            res_flash = json.loads(out_f.getvalue())
            self.assertEqual(res_flash["primary_model"], "gemini-3.8-flash-medium")
            self.assertEqual(res_flash["primary_model_mode"], "flash")
            self.assertNotEqual(res_argon["run_id"], res_flash["run_id"])
            self.assertNotEqual(res_argon["checkpoint_path"], res_flash["checkpoint_path"])
            self.assertNotEqual(res_argon["report_path"], res_flash["report_path"])

    def test_run_id_isolation_preserves_c2_c3_e_and_separates_configs(self):
        vid_path = _create_synthetic_video(self.root / "iso_video.mp4", width=320, height=180, fps=5, duration_sec=4)
        sub_path = self.root / "iso_subs.json3"
        sub_path.write_text(
            json.dumps({
                "events": [
                    {"tStartMs": 300, "dDurationMs": 1000, "segs": [{"utf8": "독립 실행 테스트"}]},
                ]
            }),
            encoding="utf-8",
        )
        vdir = self.root / "data" / "videos" / self.video_id
        vdir.mkdir(parents=True, exist_ok=True)

        # Seed existing C2, C3, and E artifacts in data/videos/Tst00000075/
        c2_ckpt = {"schema_version": 1, "video_id": self.video_id, "status": "COMPLETED", "legacy": "C2"}
        c2_analysis = {"schema_version": 1, "video_id": self.video_id, "default_model": "gemini-3.8-flash-medium", "tag": "C2"}
        c3_analysis = {"schema_version": 1, "video_id": self.video_id, "default_model": "gemini-3.8-flash-medium", "tag": "C3"}
        e_analysis = {"schema_version": 1, "video_id": self.video_id, "default_model": "gemini-4-argon-high", "tag": "E"}
        c2_ver = {"schema_version": 1, "video_id": self.video_id, "tag": "C2_VER"}
        c2_md = "# Existing C2 Final Report\n"

        (vdir / "multimodal_checkpoint.json").write_text(json.dumps(c2_ckpt), encoding="utf-8")
        (vdir / "tiered_analysis.json").write_text(json.dumps(c2_analysis), encoding="utf-8")
        (vdir / "tiered_analysis_flash_blind_c3.json").write_text(json.dumps(c3_analysis), encoding="utf-8")
        (vdir / "tiered_analysis_argon_e.json").write_text(json.dumps(e_analysis), encoding="utf-8")
        (vdir / "verification.json").write_text(json.dumps(c2_ver), encoding="utf-8")
        (vdir / "final_report.md").write_text(c2_md, encoding="utf-8")

        def custom_analyzer_a(tl):
            return {
                "scene_analyses": [
                    {
                        "scene_id": u["scene_id"],
                        "start_sec": u["start_sec"],
                        "end_sec": u["end_sec"],
                        "summary": "Run A summary",
                        "spoken_summary": u["combined_transcript_text"],
                        "visual_ui_text": "UI A",
                        "confidence": 0.99,
                        "verification_status": "VERIFIED",
                    }
                    for u in tl["units"]
                ],
                "prompts_extracted": [],
                "ui_settings_extracted": [],
            }

        # Run 1: Argon model, default boards
        run1 = run_resumable_multimodal_pipeline(
            self.root,
            self.video_id,
            video_path=vid_path,
            subtitle_json3_path=sub_path,
            primary_model="argon",
            flash_analyzer=custom_analyzer_a,
            prompt_signature="prompt_v2_clean",
            panels_per_board=2,
            max_keyframes=16,
            force=True,
        )
        # Run 2: Flash model, same prompt & boards -> different run_id
        run2 = run_resumable_multimodal_pipeline(
            self.root,
            self.video_id,
            video_path=vid_path,
            subtitle_json3_path=sub_path,
            primary_model="flash",
            flash_analyzer=custom_analyzer_a,
            prompt_signature="prompt_v2_clean",
            panels_per_board=2,
            max_keyframes=16,
        )
        # Run 3: Argon model, different Evidence Board config (panels_per_board=4, max_keyframes=8) -> different run_id
        run3 = run_resumable_multimodal_pipeline(
            self.root,
            self.video_id,
            video_path=vid_path,
            subtitle_json3_path=sub_path,
            primary_model="argon",
            flash_analyzer=custom_analyzer_a,
            prompt_signature="prompt_v2_clean",
            panels_per_board=4,
            max_keyframes=8,
        )
        # Run 4: Argon model, different prompt_signature -> different run_id
        run4 = run_resumable_multimodal_pipeline(
            self.root,
            self.video_id,
            video_path=vid_path,
            subtitle_json3_path=sub_path,
            primary_model="argon",
            flash_analyzer=custom_analyzer_a,
            prompt_signature="prompt_v3_experimental",
            panels_per_board=2,
            max_keyframes=16,
        )

        run_ids = {run1["run_id"], run2["run_id"], run3["run_id"], run4["run_id"]}
        self.assertEqual(len(run_ids), 4)
        for r in (run1, run2, run3, run4):
            self.assertTrue(Path(r["checkpoint_path"]).exists())
            self.assertTrue(Path(r["analysis_path"]).exists())
            self.assertTrue(Path(r["verification_path"]).exists())
            self.assertTrue(Path(r["report_path"]).exists())

        # Verify existing C2 / C3 / E files at top-level were NEVER overwritten
        self.assertEqual(json.loads((vdir / "multimodal_checkpoint.json").read_text(encoding="utf-8")), c2_ckpt)
        self.assertEqual(json.loads((vdir / "tiered_analysis.json").read_text(encoding="utf-8")), c2_analysis)
        self.assertEqual(json.loads((vdir / "tiered_analysis_flash_blind_c3.json").read_text(encoding="utf-8")), c3_analysis)
        self.assertEqual(json.loads((vdir / "tiered_analysis_argon_e.json").read_text(encoding="utf-8")), e_analysis)
        self.assertEqual(json.loads((vdir / "verification.json").read_text(encoding="utf-8")), c2_ver)
        self.assertEqual((vdir / "final_report.md").read_text(encoding="utf-8"), c2_md)

    def test_gitignore_blocks_large_media_files(self):
        import subprocess
        repo_root = Path(__file__).resolve().parent.parent
        ignored_candidates = [
            "data/videos/Tst00000075/media/video.f401.mp4",
            "data/videos/Tst00000075/media/video.f140-1.m4a",
            "data/videos/Tst00000075/media/video.ko-orig.json3",
            "temp_download.mp4",
            "temp_audio.m4a",
            "subtitles.ko.json3",
            "partial_download.mp4.part",
        ]
        for rel in ignored_candidates:
            proc = subprocess.run(
                ["git", "-C", str(repo_root), "check-ignore", "-q", rel],
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"Expected {rel} to be ignored by .gitignore")


if __name__ == "__main__":
    unittest.main()
