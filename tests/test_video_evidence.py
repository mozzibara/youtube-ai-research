"""Video evidence tests: distinguish actual media URI from a textual URL.

No actual AGY, YouTube or Gemini network calls occur in this suite.
"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ytresearch.video_evidence import (
    _agy_tool_evidence, video_one, review_next, evidence_status, probe_video_access,
)
from ytresearch.batch import sync_sources_csv, batch_timeline, publish_batch
from ytresearch.storage import atomic_write, digest, json_text, read_json, video_dir


VID = "Tst00000075"


class FakeAgyRunner:
    binary = "agy"

    def __init__(self, root, *, tool=True, target=VID, blocked=False):
        self.root = root
        self.config = read_json(root / "config" / "agents.json")
        self.tool = tool
        self.target = target
        self.blocked = blocked
        self.calls = []

    def _invoke(self, command, role, attempt, attempts):
        self.calls.append((command, role))
        events = [{"event": "init", "init": {"tools": ["read_url_content"]}}]
        if self.tool:
            events.append({
                "event": "step_update",
                "step_update": {
                    "state": "DONE", "step_type": "tool",
                    "tool_name": "read_url_content",
                    "tool_info": {
                        "parameters": {
                            "url": f"https://www.youtube.com/watch?v={self.target}"
                        },
                        "output": "Page title and caption sample",
                    },
                },
            })
        events.append({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": (
                    "SOURCE_ACCESS_FAILED" if self.blocked else
                    "00:00 영상 시작: 실제 모델 출력이 아닌 테스트 시간 정보\n"
                    "00:18 첫 번째 옵션과 설정에 관한 기록"
                ),
            },
        })
        return subprocess.CompletedProcess(
            command, 0, "\n".join(json.dumps(x) for x in events) + "\n", "",
        )


class FakeGemini:
    def __init__(self, text=None):
        self.calls = []
        self.response = text or (
            "00:00 실제 영상 URI 전달 후 생성된 첫 설명\n"
            "00:16 플러그인 설치와 설정 예시"
        )
        self.interactions = self

    def create(self, *, model, input):
        self.calls.append((model, input))
        return SimpleNamespace(output_text=self.response)


class VideoEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        atomic_write(self.root / "config" / "agents.json", json_text({
            "extractor": {"model": "gemini-3.8-flash-medium", "effort": "medium"},
            "video_extractor": {"model": "gemini-3.8-flash-medium", "effort": "medium"},
            "agy_video_agent": "yt-research-video",
            "gemini_video_model": "gemini-3.8-flash",
            "max_chars_per_chunk": 230,
            "archive_agy_responses": True,
            "print_timeout": "10m",
        }))
        path = self.root / ".agents" / "agents" / "yt-research-video.md"
        atomic_write(path, "name: yt-research-video\ntools: [read_url_content]\n")
        csv = self.root / "sources.csv"
        atomic_write(csv, "\n".join([
            "source_id,url",
            f"S004,https://www.youtube.com/watch?v={VID}",
            "S005,https://www.youtube.com/watch?v=Tst00000049",
        ])+"\n")
        sync_sources_csv(self.root, csv, expected=2)

    def tearDown(self):
        self.temp.cleanup()

    def _source(self):
        path = video_dir(self.root, VID)
        source = {
            "video_id": VID,
            "segments": [
                {"id": "s000001", "start": 0.0, "duration": 1.0,
                 "text": "GUI 화면에서 AI 모델 선택"},
                {"id": "s000002", "start": 5.0, "duration": 1.0,
                 "text": "모델 이름과 프롬프트는 화면 확인 필요"},
            ],
        }
        txt = json_text(source)
        atomic_write(path / "transcript.json", txt)
        atomic_write(path / "transcript.sha256", digest(txt)+"\n")
        atomic_write(
            self.root / "research" / VID / "timeline" / "chunk_001.md",
            "00:00 전반부 모델 기능 설명 [s000001]\n",
        )

    def test_probe_video_access_observes_real_targeted_web_step_not_model_claim(self):
        probe_file = (self.root / ".agents" / "agents" /
                      "yt-research-source-probe.md")
        atomic_write(probe_file, "name: yt-research-source-probe\n"
                     "tools: [read_url_content]\n")
        runner = FakeAgyRunner(self.root)
        diag = probe_video_access(self.root, VID, runner=runner)
        self.assertTrue(diag["found"])
        self.assertTrue(diag["probe_cli_success"])
        self.assertTrue(diag["probe_access_observed"])
        self.assertEqual(diag["successful_target_web_steps"], 1)
        cmd = runner.calls[0][0]
        self.assertIn("yt-research-source-probe", cmd)
        self.assertIn("--print-timeout", cmd)
        self.assertIn("90s", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertFalse((self.root / "research" / VID /
                          "video_evidence" / "full.md").exists())

    def test_probe_video_access_with_no_tool_never_claims_video_access(self):
        probe_file = (self.root / ".agents" / "agents" /
                      "yt-research-source-probe.md")
        atomic_write(probe_file, "name: yt-research-source-probe\n"
                     "tools: [read_url_content]\n")
        runner = FakeAgyRunner(self.root, tool=False)
        diag = probe_video_access(self.root, VID, runner=runner)
        self.assertTrue(diag["probe_cli_success"])
        self.assertFalse(diag["probe_access_observed"])
        self.assertEqual(diag["successful_target_web_steps"], 0)
        self.assertEqual(diag["next_action"], "NO_COMPLETED_TOOLS_CHECK_AGENT_REASONING")
        self.assertFalse((self.root / "research" / VID /
                          "video_evidence" / "full.md").exists())

    def test_actual_completed_tool_step_is_required_for_gy_web_inspection(self):
        runner = FakeAgyRunner(self.root, tool=False)
        with self.assertRaisesRegex(RuntimeError, "AGY_VIDEO_SOURCE_NOT_OBSERVED"):
            video_one(self.root, VID, mode="full", runner=runner)
        self.assertFalse(
            (self.root / "research" / VID / "video_evidence" / "full.md").exists()
        )
        self.assertTrue(
            list((self.root / "data" / "diagnostics" / "agy" / VID).rglob("summary.json"))
        )

    def test_tool_target_must_be_actual_requested_video_and_denial_is_fatal(self):
        runner = FakeAgyRunner(self.root, tool=True, target="AAAAAAAAAAA")
        with self.assertRaisesRegex(RuntimeError, "AGY_VIDEO_SOURCE_NOT_OBSERVED"):
            video_one(self.root, VID, runner=runner)
        runner = FakeAgyRunner(self.root, tool=True, blocked=True)
        with self.assertRaisesRegex(RuntimeError, "AGY_VIDEO_SOURCE_NOT_OBSERVED"):
            video_one(self.root, VID, runner=runner)

    def test_gy_web_evidence_saved_as_unverified_not_video_transcript(self):
        runner = FakeAgyRunner(self.root)
        meta = video_one(self.root, VID, runner=runner)
        self.assertEqual(meta["provider"], "agy")
        self.assertEqual(meta["access_status"], "web_access_observed_not_media_proof")
        self.assertTrue(meta["review_required"])
        self.assertEqual(meta["tool_names"], ["read_url_content"])
        self.assertNotIn("--dangerously-skip-permissions", runner.calls[0][0])
        self.assertNotIn("yt-research-no-tools", runner.calls[0][0])
        status = evidence_status(self.root, VID)
        self.assertEqual(status["status"], "video_evidence_review_required")
        self.assertFalse((self.root / "research" / VID / "transcript.md").exists())
        self.assertEqual(
            video_one(self.root, VID, runner=FakeAgyRunner(self.root)),
            meta,
        )

    def test_direct_gemini_passes_true_video_uri_input_not_text_url(self):
        fake = FakeGemini()
        meta = video_one(self.root, VID, provider="gemini-url", client=fake)
        self.assertEqual(meta["access_status"],
                         "video_uri_submitted_not_fully_verified")
        self.assertEqual(len(fake.calls), 1)
        model, parts = fake.calls[0]
        self.assertEqual(model, "gemini-3.8-flash")
        self.assertEqual(parts[-1], {
            "type": "video",
            "uri": f"https://www.youtube.com/watch?v={VID}",
        })
        self.assertEqual(meta["mode"], "full")

    def test_agy_and_gemini_full_results_have_separate_immutable_checkpoints(self):
        agy = video_one(self.root, VID, runner=FakeAgyRunner(self.root))
        gemini = video_one(
            self.root, VID, provider="gemini-url", client=FakeGemini()
        )
        self.assertEqual(agy["unit"], "full")
        self.assertEqual(gemini["unit"], "full_gemini")
        folder = self.root / "research" / VID / "video_evidence"
        self.assertTrue((folder / "full.md").is_file())
        self.assertTrue((folder / "full_gemini.md").is_file())
        self.assertEqual(
            evidence_status(self.root, VID)["source_access"],
            "video_uri_submitted_not_fully_verified",
        )
        # The purpose here is provenance and publication, not processing
        # S005. A real batch would skip this completed S004 and attempt S005.
        # Never let a unit test launch the user's AGY CLI against that URL.
        from ytresearch.batch import _write_index, load_active
        with (
            patch("ytresearch.batch.video_one", side_effect=AssertionError("unexpected AGY call")),
            patch("ytresearch.batch.collect_one", side_effect=AssertionError("unexpected YouTube call")),
        ):
            idx = _write_index(self.root, load_active(self.root, expected=2))
            with self.assertRaisesRegex(RuntimeError, "PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED"):
                publish_batch(self.root, expected=2)
        self.assertEqual(idx, read_json(self.root / "research" / "index.json"))
        self.assertEqual(idx["video_evidence_review_required"], 1)
        self.assertEqual(idx["sources"][0]["status"],
                         "video_evidence_review_required")
        self.assertEqual(idx["sources"][1]["status"], "not_started")
        self.assertTrue((folder / "full_gemini.md").is_file())
        self.assertTrue((folder / "full.md").is_file())

    def test_video_only_never_forges_source_segment_ids(self):
        fake = FakeGemini("00:01 자막 s000001과 동일하다고 주장")
        with self.assertRaisesRegex(RuntimeError, "VIDEO_UNGROUNDED_SEGMENT_IDS"):
            video_one(self.root, VID, provider="gemini-url", client=fake)
        self.assertIsNone(evidence_status(self.root, VID))

    def test_review_complements_exact_subtitle_chunk_without_overwrite(self):
        self._source()
        runner = FakeAgyRunner(self.root)
        original = (self.root / "research" / VID / "timeline" /
                    "chunk_001.md").read_bytes()
        reviews = review_next(self.root, VID, runner=runner)
        self.assertEqual(reviews, [1])
        meta = read_json(
            self.root / "research" / VID / "video_evidence" /
            "review_chunk_001.json"
        )
        self.assertEqual(meta["mode"], "review")
        self.assertEqual(meta["source_sha256"],
                         digest(json_text(read_json(
                             video_dir(self.root, VID) / "transcript.json"
                         ))))
        self.assertEqual(
            (self.root / "research" / VID / "timeline" / "chunk_001.md").read_bytes(),
            original,
        )
        prompt = runner.calls[0][0][2]
        self.assertIn("s000001", prompt)
        self.assertIn("기존 AGY 자막 분석", prompt)
        self.assertIn(VID, prompt)
        # Re-running does not spend model tokens.
        self.assertEqual(review_next(self.root, VID, runner=runner), [])
        self.assertEqual(len(runner.calls), 1)
        integrated = self.root / "research" / VID / "video_evidence" / "combined_timeline.md"
        self.assertTrue(integrated.is_file())
        combined = integrated.read_text(encoding="utf-8")
        self.assertIn("자막 기반 초기 시간순 기록", combined)
        self.assertIn("영상 접근 기반 보완 관찰", combined)
        self.assertIn("NOT proof of complete audiovisual access", combined)

    def test_gy_video_only_batch_never_calls_youtube_transcript_api(self):
        fake = FakeAgyRunner(self.root)
        def run_direct(root, vid, *, provider, mode):
            return video_one(root, vid, provider=provider, mode=mode,
                             runner=FakeAgyRunner(root, target=vid))
        with patch("ytresearch.batch.collect_one",
                   side_effect=AssertionError("No transcript fetch allowed")), \
             patch("ytresearch.batch.video_one", side_effect=run_direct), \
             contextlib.redirect_stdout(io.StringIO()):
            idx = batch_timeline(
                self.root, expected=2, max_videos=1, source_mode="agy-only"
            )
        self.assertEqual(idx["video_evidence_review_required"], 1)
        self.assertEqual(idx["sources"][0]["status"],
                         "video_evidence_review_required")
        self.assertFalse(idx["sources"][0]["gpt_report"])
        self.assertEqual(idx["sources"][1]["status"], "not_started")
        with self.assertRaisesRegex(RuntimeError, "PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED"):
            publish_batch(self.root, expected=2)
        self.assertTrue((self.root / "research" / VID / "video_evidence" / "full.md").is_file())
        self.assertTrue((self.root / "research" / VID / "video_evidence" / "full.json").is_file())

    def test_no_timestamp_is_an_error_not_accepted_as_full_timeline(self):
        fake = FakeGemini("세부 기술 내용은 언급되었지만 시간이 없습니다")
        with self.assertRaisesRegex(RuntimeError, "VIDEO_TIMESTAMPS_NOT_PRESENT"):
            video_one(self.root, VID, provider="gemini-url", client=fake)


if __name__ == "__main__":
    unittest.main()
