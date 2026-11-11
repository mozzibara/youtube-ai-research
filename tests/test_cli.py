"""Regression tests for main() CLI dispatch and import scope handling."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ytresearch import __main__ as cli
from ytresearch.pipeline import import_csv


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = self.root / "config"
        config.mkdir()
        (config / "agents.json").write_text(json.dumps({
            "extractor": {"model": "example", "effort": "medium"},
            "analyzer": {"model": "example", "effort": "high"},
            "auditor": {"model": "example", "effort": "high"},
        }), encoding="utf-8")
        csv = self.root / "sources.csv"
        csv.write_text("source_id,url\nS001,https://www.youtube.com/watch?v=Tst00000054\n", encoding="utf-8")
        import_csv(self.root, csv)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_cli_uses_module_level_list_videos(self):
        with patch.object(sys, "argv", ["ytresearch", "--root", str(self.root), "status"]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                rc = cli.main()
        self.assertEqual(rc, 0)
        self.assertIn("Tst00000054", output.getvalue())

    def test_run_cli_dispatches_all_four_stages(self):
        with patch.object(sys, "argv", ["ytresearch", "--root", str(self.root), "run", "--limit", "1"]), \
             patch.object(cli, "collect_one", return_value="COLLECTED") as collect, \
             patch.object(cli, "extract_one", return_value=(1, 1)) as extract, \
             patch.object(cli, "analyze_one", return_value=19) as analyze, \
             patch.object(cli, "audit_one", return_value=("MODEL_PASS_REVIEW_REQUIRED", 0)) as audit, \
             contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main()
        self.assertEqual(rc, 0)
        collect.assert_called_once()
        extract.assert_called_once()
        analyze.assert_called_once()
        audit.assert_called_once()

    def test_import_vtt_uses_active_csv_without_legacy_sqlite_registration(self):
        from ytresearch.batch import sync_sources_csv
        from ytresearch.storage import read_json, video_dir

        video_id = "Tst00000075"
        sources = self.root / "sources.csv"
        sources.write_text(
            f"source_id,url\nS004,https://www.youtube.com/watch?v={video_id}\n",
            encoding="utf-8",
        )
        sync_sources_csv(self.root, sources, expected=1)
        vtt = self.root / "provided.vtt"
        vtt.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
            "설치 옵션 --safe 확인\n\n",
            encoding="utf-8",
        )
        args = [
            "ytresearch", "--root", str(self.root), "import-vtt",
            "--video-id", video_id,
            "--file", str(vtt),
            "--language", "ko",
            "--expected", "1",
        ]
        with patch.object(sys, "argv", args), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 0)
        data = read_json(video_dir(self.root, video_id) / "transcript.json")
        self.assertEqual(data["video_id"], video_id)
        self.assertEqual(data["segments"][0]["text"], "설치 옵션 --safe 확인")
        self.assertEqual(data["source_method"], "manual_webvtt")
        self.assertIn("Imported VTT", output.getvalue())

    def test_import_vtt_rejects_id_not_in_legacy_or_active_registry(self):
        from ytresearch.batch import sync_sources_csv

        video_id = "YYYYYYYYYYY"
        source_csv = self.root / "sources.csv"
        sync_sources_csv(self.root, source_csv, expected=1)
        vtt = self.root / "provided.vtt"
        vtt.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nsource\n",
            encoding="utf-8",
        )
        args = [
            "ytresearch", "--root", str(self.root), "import-vtt",
            "--video-id", video_id, "--file", str(vtt), "--expected", "1",
        ]
        with patch.object(sys, "argv", args), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "IMPORT_VTT_UNREGISTERED_VIDEO"):
                cli.main()

    def test_audit_without_transcript_preserves_prior_collected_stage(self):
        from contextlib import closing
        from ytresearch.storage import connect, update_stage_at_root
        video_id = "Tst00000054"
        update_stage_at_root(self.root, video_id, "COLLECTED")
        args = ["ytresearch", "--root", str(self.root), "audit", "--video-id", video_id]
        with (
            patch.object(sys, "argv", args),
            patch.object(cli, "audit_one", side_effect=AssertionError("no model")),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(cli.main(), 1)
        self.assertIn("PREREQUISITE_MISSING", err.getvalue())
        self.assertIn("collect --video-id", err.getvalue())
        with closing(connect(self.root)) as db:
            stage = db.execute("SELECT stage FROM videos WHERE video_id=?", (video_id,)).fetchone()[0]
        self.assertEqual(stage, "COLLECTED")

    def test_audit_missing_extraction_guides_prior_step_without_model_call(self):
        video_id = "Tst00000054"
        folder = self.root / "data" / "videos" / video_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "transcript.json").write_text("{}", encoding="utf-8")
        (folder / "transcript.sha256").write_text("synthetic", encoding="utf-8")
        args = ["ytresearch", "--root", str(self.root), "audit", "--video-id", video_id]
        with (
            patch.object(sys, "argv", args),
            patch.object(cli, "audit_one", side_effect=AssertionError("no model")),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(cli.main(), 1)
        self.assertIn("extraction.json", err.getvalue())
        self.assertIn("extract --video-id", err.getvalue())

    def test_batch_missing_audit_prerequisites_returns_nonzero(self):
        args = ["ytresearch", "--root", str(self.root), "audit", "--limit", "1"]
        with (
            patch.object(sys, "argv", args),
            patch.object(cli, "audit_one", side_effect=AssertionError("no model")),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(cli.main(), 1)
        self.assertIn("PREREQUISITE_MISSING", err.getvalue())

    def test_publish_batch_cli_is_disabled_before_config_or_data_access(self):
        nonexisting = self.root / "missing-external-root"
        flags = (
            (),
            ("--commit",),
            ("--commit", "--push", "--acknowledge-rights"),
        )
        for args in flags:
            argv = ["ytresearch", "--root", str(nonexisting), "publish-batch", *args]
            with self.subTest(args=args), \
                 patch.object(sys, "argv", argv), \
                 patch.object(cli, "publish_batch", side_effect=AssertionError("must not execute")), \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as ctx:
                    cli.main()
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED", err.getvalue())
        self.assertFalse(nonexisting.exists())

    def test_limit_uses_source_order_not_video_id(self):
        additional = self.root / "additional.csv"
        additional.write_text("source_id,url\nS002,https://youtube.com/watch?v=Tst00000002\n", encoding="utf-8")
        import_csv(self.root, additional)
        from ytresearch.pipeline import list_videos
        self.assertEqual(list_videos(self.root, limit=1)[0]["video_id"], "Tst00000054")


if __name__ == "__main__":
    unittest.main()
