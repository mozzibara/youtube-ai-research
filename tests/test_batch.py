"""77-video playlist/batch/publishing tests: no YouTube network or AI calls."""
import contextlib
import io
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ytresearch.batch import (
    ACTIVE_REGISTRY, batch_timeline, load_active, publish_batch,
    sync_playlist, sync_sources_csv, _existing_export,
)
from ytresearch.storage import atomic_write, digest, json_text, read_json, video_dir
from ytresearch.timeline import _timeline_prompt


class ActivePlaylistBatchTests(unittest.TestCase):
    ids = ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        atomic_write(
            self.root / "config" / "agents.json",
            json_text({"max_chars_per_chunk": 2200}),
        )
        self.url = "https://www.youtube.com/playlist?list=PL00000000000000000000000000000000"
        self.entries = [{"id": self.ids[0], "title": "ComfyUI"},
                        {"id": "DDDDDDDDDDD", "title": "Private video"},
                        {"id": self.ids[1], "title": "Video B"},
                        None,
                        {"id": self.ids[0], "title": "Duplicate"},
                        {"id": self.ids[2], "title": "Video C"}]

    def tearDown(self):
        self.temp.cleanup()

    def _sync(self, expected=3):
        return sync_playlist(
            self.root, self.url, expected=expected,
            extractor=lambda _: {"entries": self.entries},
        )

    def _save_export(self, vid):
        folder = self.root / "research" / vid
        source = {"video_id": vid, "segments": [
            {"id": "s000001", "start": 0.0, "duration": 1.0,
             "text": "정확한 기술 내용"},
        ]}
        raw = json_text(source)
        path = video_dir(self.root, vid)
        atomic_write(path / "transcript.json", raw)
        atomic_write(path / "transcript.sha256", digest(raw) + "\n")
        atomic_write(folder / "transcript.md",
                     "[s000001 @ 00:00:00 / 0.00s] 정확한 기술 내용\n")
        atomic_write(folder / "timeline" / "chunk_001.md",
                     "영상 기술 분석이 아닌 시간순 상세 기록 [s000001]")
        atomic_write(folder / "manifest.json", json_text({
            "video_id": vid, "chunk_count": 1,
            "transcript_segments": 1, "source_sha256": digest(raw),
            "chunks": [{"file": "timeline/chunk_001.md", "segment_count": 1}],
        }))

    def _csv_rows(self, entries=None):
        rows = entries or [
            ("S001", self.ids[0]),
            ("S002", self.ids[1]),
            ("S003", self.ids[0]),  # Duplicate video, distinct source
            ("S004", self.ids[2]),
        ]
        path = self.root / "sources.csv"
        lines = ["source_id,url"]
        lines.extend(
            f"{source_id},https://www.youtube.com/watch?v={video_id}"
            for source_id, video_id in rows
        )
        atomic_write(path, "\n".join(lines) + "\n")
        return path

    def test_existing_sources_csv_is_primary_and_preserves_all_source_ids(self):
        csv_path = self._csv_rows()
        original = csv_path.read_bytes()
        result = sync_sources_csv(self.root, csv_path, expected=4)
        self.assertEqual(result["source_entries_count"], 4)
        self.assertEqual(result["unique_video_count"], 3)
        self.assertEqual(result["duplicate_source_entries"], 1)
        self.assertEqual(
            [x["source_id"] for x in result["entries"]],
            ["S001", "S002", "S003", "S004"],
        )
        self.assertEqual(
            [x["video_id"] for x in result["sources"]], self.ids
        )
        self.assertEqual(result["sources"][0]["source_ids"], ["S001", "S003"])
        self.assertEqual(result["sources"][0]["source_id"], "S001")
        self.assertEqual(
            load_active(self.root, expected=4)["source_origin"], "sources_csv"
        )
        self.assertEqual(csv_path.read_bytes(), original)
        self.assertIsNone(result["playlist_url"])
        from ytresearch.batch import _write_index
        index = _write_index(self.root, load_active(self.root, expected=4))
        self.assertEqual(read_json(self.root / "research" / "index.json"), index)
        # The public Git publisher must not be used for index generation.
        with self.assertRaisesRegex(RuntimeError, "PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED"):
            publish_batch(self.root, expected=4)
        self.assertEqual(index["source_count"], 3)
        self.assertEqual(index["source_entries_count"], 4)
        self.assertEqual(index["duplicate_source_entries"], 1)
        self.assertEqual(index["sources"][0]["source_id"], "S001")
        self.assertEqual(index["sources"][0]["source_ids"], ["S001", "S003"])
        self.assertEqual(
            sorted(sid for entry in index["sources"] for sid in entry["source_ids"]),
            ["S001", "S002", "S003", "S004"],
        )

    def test_csv_count_mismatch_does_not_overwrite_existing_snapshot(self):
        csv_path = self._csv_rows()
        sync_sources_csv(self.root, csv_path, expected=4)
        original = (self.root / ACTIVE_REGISTRY).read_bytes()
        with self.assertRaisesRegex(RuntimeError, "CSV_SOURCE_COUNT_MISMATCH"):
            sync_sources_csv(self.root, csv_path, expected=77)
        self.assertEqual(
            (self.root / ACTIVE_REGISTRY).read_bytes(), original
        )

    def test_csv_duplicate_source_id_or_bad_url_fails_closed(self):
        csv_path = self._csv_rows()
        rows = [
            ("S001", self.ids[0]),
            ("S001", self.ids[1]),
        ]
        self._csv_rows(rows)
        with self.assertRaisesRegex(ValueError, "CSV_DUPLICATE_SOURCE_ID"):
            sync_sources_csv(self.root, csv_path, expected=2)
        self._csv_rows([
            ("S001", self.ids[0]),
            ("S002", "invalid-video-id"),
        ])
        with self.assertRaisesRegex(ValueError, "CSV_VIDEO_URL_INVALID"):
            sync_sources_csv(self.root, csv_path, expected=2)
        self.assertFalse((self.root / ACTIVE_REGISTRY).exists())

    def test_csv_alias_integrity_validation_rejects_tampering(self):
        csv_path = self._csv_rows()
        sync_sources_csv(self.root, csv_path, expected=4)
        data = read_json(self.root / ACTIVE_REGISTRY)
        data["sources"][0]["source_ids"] = ["S001"]
        atomic_write(self.root / ACTIVE_REGISTRY, json_text(data))
        with self.assertRaisesRegex(RuntimeError, "ACTIVE_CSV_ALIAS_MISMATCH"):
            load_active(self.root, expected=4)

    def test_sync_preserves_order_and_separates_private_deleted_duplicates(self):
        registry = self._sync()
        self.assertEqual(registry["accessible_count"], 3)
        self.assertEqual([x["video_id"] for x in registry["sources"]], self.ids)
        self.assertEqual([x["playlist_position"] for x in registry["sources"]],
                         [1, 3, 6])
        self.assertEqual(len(registry["unavailable_positions"]), 3)
        self.assertEqual(load_active(self.root, expected=3)["sources"],
                         registry["sources"])
        self.assertFalse((self.root / "sources.csv").exists())

    def test_wrong_active_count_never_overwrites_previous_snapshot(self):
        self._sync()
        before = (self.root / ACTIVE_REGISTRY).read_text(encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "PLAYLIST_COUNT_MISMATCH"):
            self._sync(expected=77)
        self.assertEqual((self.root / ACTIVE_REGISTRY).read_text(encoding="utf-8"),
                         before)

    def test_unavailable_and_hidden_ids_are_not_invented(self):
        self.entries = [
            {"id": "ABCDEFGHIJK", "availability": "private", "title": "No access"},
            {"id": self.ids[1], "availability": "public"},
            None,
        ]
        result = sync_playlist(
            self.root, self.url, expected=1,
            extractor=lambda _: {"entries": self.entries},
        )
        self.assertEqual([x["video_id"] for x in result["sources"]],
                         [self.ids[1]])
        self.assertEqual([x["reason"] for x in result["unavailable_positions"]],
                         ["restricted", "unavailable"])

    def test_load_active_catches_duplicate_ids_and_source_tampering(self):
        self._sync()
        obj = read_json(self.root / ACTIVE_REGISTRY)
        obj["sources"][1]["video_id"] = self.ids[0]
        atomic_write(self.root / ACTIVE_REGISTRY, json_text(obj))
        with self.assertRaisesRegex(RuntimeError, "ACTIVE_PLAYLIST_DUPLICATE"):
            load_active(self.root, expected=3)

    def test_batch_skips_ready_continues_after_failure_and_reports_remaining(self):
        self._sync()
        self._save_export(self.ids[0])
        (self.root / "research" / self.ids[0] / "final").mkdir()
        atomic_write(self.root / "research" / self.ids[0] / "final" / "report.md",
                     "GPT Web reviewed 19 sections")
        collected, ran_timeline = [], []
        def fake_collect(root, vid):
            collected.append(vid)
            if vid == self.ids[1]:
                raise RuntimeError("CAPTIONS_UNAVAILABLE: private transcript")
            self._save_export(vid)
        def fake_timeline(root, vid):
            ran_timeline.append(vid)
            return 1
        with patch("ytresearch.batch.collect_one", side_effect=fake_collect), \
             patch("ytresearch.batch.timeline_one", side_effect=fake_timeline), \
             patch("ytresearch.batch.export_timeline", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()):
            result = batch_timeline(self.root, expected=3, max_videos=2)
        self.assertEqual(collected, self.ids[1:])
        self.assertEqual(ran_timeline, [self.ids[2]])
        self.assertEqual(result["ready_for_gpt_web"], 2)
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(result["final_report_count"], 1)
        self.assertEqual(result["sources"][1]["status"], "blocked")
        self.assertEqual(result["sources"][1]["error_code"], "CAPTIONS_UNAVAILABLE")
        self.assertIn("이번 실행에서 타임라인 추출 완료한 소스 목록",
                      output.getvalue())
        self.assertTrue((self.root / "research" / "index.json").is_file())

    def test_failed_source_stays_blocked_until_explicit_retry(self):
        self._sync()
        self._save_export(self.ids[0])
        with patch("ytresearch.batch.collect_one",
                   side_effect=RuntimeError("CAPTIONS_UNAVAILABLE: no caption")), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            first = batch_timeline(self.root, expected=3, max_videos=1)
        self.assertEqual(first["sources"][1]["status"], "blocked")
        # Mark the next unblocked source complete so the following run has
        # nothing to execute unless the blocked entry is explicitly retried.
        self._save_export(self.ids[2])
        with patch("ytresearch.batch.collect_one",
                   side_effect=AssertionError("blocked videos should be skipped")), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            next_run = batch_timeline(self.root, expected=3, max_videos=1)
        self.assertEqual(next_run["sources"][1]["status"], "blocked")
        self.assertEqual(next_run["sources"][1]["error_code"], "CAPTIONS_UNAVAILABLE")
        # Explicitly retry only the isolated video.
        def resume_collect(root, vid):
            self.assertEqual(vid, self.ids[1])
            self._save_export(vid)
        with patch("ytresearch.batch.collect_one", side_effect=resume_collect), \
             patch("ytresearch.batch.timeline_one", return_value=1), \
             patch("ytresearch.batch.export_timeline", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            retried = batch_timeline(self.root, expected=3, max_videos=1,
                                     retry_blocked=True)
        self.assertEqual(retried["sources"][1]["status"], "ready_for_gpt_web")

    def test_youtube_request_block_stops_other_fetches_and_remains_paused(self):
        class RequestBlocked(Exception):
            pass
        self._sync()
        self._save_export(self.ids[0])
        attempted = []
        def blocked_collect(root, vid):
            attempted.append(vid)
            raise RequestBlocked("YouTube blocked requests from the IP")
        with patch("ytresearch.batch.collect_one", side_effect=blocked_collect), \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            first = batch_timeline(self.root, expected=3, max_videos=3)
        self.assertEqual(attempted, [self.ids[1]])
        self.assertEqual(first["sources"][1]["status"], "paused_network")
        self.assertEqual(first["sources"][1]["error_code"], "REQUESTBLOCKED")
        self.assertEqual(first["sources"][2]["status"], "not_started")
        self.assertEqual(first["paused_network_count"], 1)
        self.assertTrue(first["network_paused"])
        self.assertIn("다른 영상의 YouTube 자막 수집을 중단", stderr.getvalue())
        # Repeat without explicit retry must make zero YouTube requests.
        with patch("ytresearch.batch.collect_one",
                   side_effect=AssertionError("must not request YouTube again")), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            second = batch_timeline(self.root, expected=3, max_videos=3)
        self.assertEqual(second["paused_network_count"], 1)
        self.assertIn("NETWORK_PAUSED", stderr.getvalue())

    def test_old_blocked_requestblocked_rows_upgrade_to_network_pause(self):
        self._sync()
        self._save_export(self.ids[0])
        record = {
            "video_id": self.ids[1], "status": "blocked",
            "error_code": "REQUESTBLOCKED",
        }
        atomic_write(
            self.root / "research" / "index.json",
            json_text({"sources": [record]}),
        )
        with patch("ytresearch.batch.collect_one",
                   side_effect=AssertionError("no YouTube request")), \
             contextlib.redirect_stdout(io.StringIO()):
            index = batch_timeline(self.root, expected=3, max_videos=3)
        self.assertTrue(index["network_paused"])
        self.assertEqual(index["sources"][1]["status"], "paused_network")
        self.assertEqual(index["sources"][2]["status"], "not_started")

    def test_explicit_network_retry_probes_once_then_resumes_after_success(self):
        class IpBlocked(Exception):
            pass
        self._sync()
        self._save_export(self.ids[0])
        atomic_write(
            self.root / "research" / "index.json",
            json_text({"sources": [{
                "video_id": self.ids[1], "status": "paused_network",
                "error_code": "IPBLOCKED",
            }]}),
        )
        attempts = []
        def denied_collect(root, vid):
            attempts.append(vid)
            raise IpBlocked("still blocked")
        with patch("ytresearch.batch.collect_one", side_effect=denied_collect), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            denied = batch_timeline(self.root, expected=3, max_videos=3,
                                    retry_blocked=True)
        self.assertEqual(attempts, [self.ids[1]])
        self.assertTrue(denied["network_paused"])
        # When the restriction has cleared, reuse existing checkpoints and
        # process precisely one requested video (max_videos=1).
        def recovered_collect(root, vid):
            self.assertEqual(vid, self.ids[1])
            self._save_export(vid)
        with patch("ytresearch.batch.collect_one", side_effect=recovered_collect), \
             patch("ytresearch.batch.timeline_one", return_value=1), \
             patch("ytresearch.batch.export_timeline", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            success = batch_timeline(self.root, expected=3, max_videos=1,
                                     retry_blocked=True)
        self.assertFalse(success["network_paused"])
        self.assertEqual(success["ready_for_gpt_web"], 2)
        self.assertEqual(success["sources"][2]["status"], "not_started")

    def test_batch_checkpoint_recovers_and_skips_previous_exports(self):
        self._sync()
        for vid in self.ids:
            self._save_export(vid)
        with patch("ytresearch.batch.collect_one",
                   side_effect=AssertionError("must not recollect")), \
             patch("ytresearch.batch.timeline_one",
                   side_effect=AssertionError("must not re-run AI")), \
             contextlib.redirect_stdout(io.StringIO()):
            result = batch_timeline(self.root, expected=3, max_videos=1)
        self.assertEqual(result["ready_for_gpt_web"], 3)

    def test_corrupt_export_is_blocked_without_aborting_other_sources(self):
        self._sync()
        self._save_export(self.ids[0])
        (self.root / "research" / self.ids[0] / "manifest.json").write_text(
            "{}", encoding="utf-8"
        )
        self._save_export(self.ids[1])
        with patch("ytresearch.batch.collect_one",
                   side_effect=RuntimeError("CAPTIONS_UNAVAILABLE")), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            result = batch_timeline(self.root, expected=3, max_videos=1)
        self.assertEqual(result["sources"][0]["status"], "blocked")
        self.assertEqual(result["sources"][1]["status"], "ready_for_gpt_web")

    def test_publish_is_disabled_for_preview_and_rights_flags(self):
        scenarios = (
            {},
            {"commit": True},
            {"commit": True, "push": True},
            {"commit": True, "acknowledge_rights": True},
            {"commit": True, "push": True, "acknowledge_rights": True,
             "allow_whitespace_warnings": True},
        )
        for options in scenarios:
            with self.subTest(options=options), \
                 patch("ytresearch.batch.load_active", side_effect=AssertionError("registry access")):
                with self.assertRaisesRegex(RuntimeError, "PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED"):
                    publish_batch(self.root, expected=3, **options)

    def test_publish_cannot_write_even_when_private_synthetic_data_exists(self):
        private = self.root / "research" / self.ids[0] / "transcript.md"
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text("synthetic private text", encoding="utf-8")
        before = private.read_bytes()
        with patch("ytresearch.batch.subprocess.run",
                   side_effect=AssertionError("git subprocess must never run")):
            with self.assertRaisesRegex(RuntimeError, "PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED"):
                publish_batch(self.root, expected=3, commit=True, push=True,
                              acknowledge_rights=True)
        self.assertEqual(private.read_bytes(), before)
        self.assertFalse((self.root / ".git").exists())

    def test_publish_rejects_alternate_root_and_force_like_flags(self):
        other_root = self.root / "other-private-clone"
        for root in (self.root, other_root, Path("/unlikely-test-path")):
            with self.subTest(root=str(root)):
                with self.assertRaisesRegex(RuntimeError, "PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED"):
                    publish_batch(root, commit=True, push=True, expected=0,
                                  acknowledge_rights=True,
                                  allow_whitespace_warnings=True)

    def test_publish_cannot_stage_existing_synthetic_git_files(self):
        # Public policy must be source-wide, not dependent on .gitignore or origin.
        (self.root / "research").mkdir(exist_ok=True)
        (self.root / "research" / "index.json").write_text("{}", encoding="utf-8")
        with patch("ytresearch.batch.subprocess.run",
                   side_effect=AssertionError("git process must never run")):
            with self.assertRaisesRegex(RuntimeError, "PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED"):
                publish_batch(self.root, expected=1, commit=True,
                              acknowledge_rights=True)

    def test_negative_filter_and_entities_are_written_into_real_prompt(self):
        group = [{"id": "s000001", "start": 0.0, "duration": 1.0,
                  "text": "저희 채널 구독하고 다음은 컨텍스트 길이 4096으로 변경하세요"}]
        prompt = _timeline_prompt(group, 1, 1)
        for phrase in ("네거티브", "구독", "스폰서", "Skills",
                       "Plugins", "MCP", "프롬프트/명령어", "실전 Workflow",
                       "원본 자막", "JSON Schema는 없습니다"):
            self.assertIn(phrase, prompt)
        self.assertIn("s000001", prompt)


if __name__ == "__main__":
    unittest.main()
