"""Offline, synthetic-only WP1 contract tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ytresearch import research_batch as b


class ResearchBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source.mp4").write_bytes(b"synthetic media fixture")
        (self.root / "ref.txt").write_text("alpha beta", encoding="utf-8")
        self.model = "gemini-3.8-flash-medium"

    def prepare(self, **kwargs):
        with patch.object(b, "check_media", return_value={"duration_seconds": 30, "bytes": 23}):
            return b.prepare(self.root, "source.mp4", "ref.txt",
                             models=[self.model], **kwargs)

    def test_prepare_no_model_and_idempotent(self):
        with patch.object(b, "guided_run", side_effect=AssertionError("live forbidden")):
            bid = self.prepare()
            self.assertEqual(bid, self.prepare())
            root, base, plan = b.load_plan(self.root, bid)
            work, media = b.workspace(root, base, plan["jobs"][0], plan)
            self.assertEqual([p.name for p in work.iterdir()], ["sample.mp4"])
            self.assertEqual(media.read_bytes(), b"synthetic media fixture")
            self.assertEqual(b.report(root, bid)["reserved_attempts"], 0)

    def test_capture_report_and_duplicate_claim(self):
        bid = self.prepare(reference_kind="CAPTION_PROXY")
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        with self.assertRaises(FileExistsError):
            b.claim(root, base, job)
        self.assertEqual(b.report(root, bid)["jobs"][0]["status"], "BLOCKED_UNSETTLED_CLAIM")
        raw = b.json_bytes({"transcript": "alpha beta", "uncertainties": [], "status": "COMPLETE"})
        self.assertEqual(b.capture(root, base, job, raw, 2.0), "CAPTURED")
        result = b.report(root, bid)
        self.assertEqual(result["reference_kind"], "CAPTION_PROXY")
        self.assertEqual(result["jobs"][0]["metrics"]["raw_cer"]["rate"], 0)
        self.assertFalse(result["provider_ingress_verified"])
        self.assertEqual(result["reserved_attempts"], 1)
        self.assertEqual(b.report(root, bid), result)

    def test_invalid_capture_preserved(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        self.assertEqual(b.capture(root, base, job, b"not JSON", 1.0), "INVALID_CAPTURE")
        self.assertEqual((root / base / "j01/response.txt").read_bytes(), b"not JSON")
        self.assertEqual(b.report(root, bid)["jobs"][0]["status"], "INVALID_CAPTURE")

    def test_reference_and_response_tamper_blocked(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        b.capture(root, base, job, b'{"transcript":"alpha","uncertainties":[],"status":"PARTIAL"}', 1)
        path = root / base / "j01/response.txt"
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(b.BatchError, "CAPTURE_HASH_MISMATCH"):
            b.report(root, bid)
        (root / base / "evaluation-only/reference.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(b.BatchError, "REFERENCE_HASH_MISMATCH"):
            b.report(root, bid)

    def test_workspace_contamination_and_hash_tamper(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        work, media = b.workspace(root, base, job, plan)
        (work / "extra.txt").write_text("x")
        with self.assertRaisesRegex(b.BatchError, "WORKSPACE_CONTENT_MISMATCH"):
            b.workspace(root, base, job, plan)
        (work / "extra.txt").unlink()
        media.write_bytes(b"changed")
        with self.assertRaisesRegex(b.BatchError, "MEDIA_HASH_MISMATCH"):
            b.workspace(root, base, job, plan)

    def test_path_and_model_guard(self):
        with self.assertRaises(b.BatchError):
            b.safe_path(self.root, "../escape")
        with self.assertRaises(b.BatchError):
            b.safe_path(self.root, str(self.root / "source.mp4"))
        with self.assertRaisesRegex(b.BatchError, "MODEL_ALLOWLIST_REQUIRED"):
            b.prepare(self.root, "source.mp4", models=["unexpected"])
        (self.root / ".git").mkdir()
        with self.assertRaises(ValueError):
            b.safe_root(self.root)

    def test_response_validation_and_metrics(self):
        for invalid in (
            b'[]',
            b'{"transcript":"a","transcript":"b","uncertainties":[],"status":"COMPLETE"}',
            b'{"transcript":"","uncertainties":[],"status":"COMPLETE"}',
            b'{"transcript":"a","uncertainties":[],"status":"UNAVAILABLE"}',
            b'{"transcript":"a","uncertainties":[1],"status":"PARTIAL"}',
        ):
            with self.assertRaises(b.BatchError):
                b.validate_response(invalid)
        self.assertEqual(b.metrics("abc", "adc")["raw_cer"]["edits"], 1)
        self.assertEqual(b.metrics("a b.", "ab")["normalized_cer"]["rate"], 0)
        self.assertIsNone(b.error_rate("", "")["rate"])
        with patch.object(b, "MAX_EDIT_CELLS", 1):
            self.assertIsNone(b.error_rate("ab", "ab")["rate"])

    def test_default_probe_has_no_prompt(self):
        from subprocess import CompletedProcess
        def fake(cmd, **kwargs):
            self.assertIn(cmd[1], ("--help", "--version"))
            return CompletedProcess(cmd, 0, b"1.5.16 --model --effort --sandbox --dangerously-skip-permissions", b"")
        with patch.object(b.shutil, "which", return_value="synthetic-agy"), patch.object(
                b.subprocess, "run", side_effect=fake) as proc:
            info = b.capability_probe()
        self.assertEqual(proc.call_count, 2)
        self.assertEqual(info["model_requests"], 0)
        self.assertEqual(info["version"], "1.5.16")
        self.assertFalse(info["headless_media_verified"])

    def test_cli_run_requires_authorization(self):
        with patch.object(b, "guided_run", side_effect=AssertionError("live forbidden")):
            self.assertEqual(b.main(["run", "--root", str(self.root),
                                     "--batch", "invalid", "--max-calls", "1"]), 2)


if __name__ == "__main__":
    unittest.main()
