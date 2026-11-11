"""Standalone public Phase 5F synthetic preflight and CLI exit contracts."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ytresearch import __main__ as cli
from ytresearch.phase5f_validation import run_phase5f_validation_suite


class Phase5F5PreflightTests(unittest.TestCase):
    def test_all_seven_disposable_offline_scenarios_pass(self):
        report = run_phase5f_validation_suite()
        self.assertEqual(report["total_scenarios"], 7, report)
        self.assertEqual(report["passed_scenarios"], 7, report)
        self.assertEqual(report["failed_scenarios"], 0, report)
        self.assertEqual([x["id"] for x in report["scenarios"]],
                         ["F01", "F02", "F03", "F04", "F05", "F06", "F07"])
        self.assertEqual(report["actual_agy_calls"], 0)
        self.assertEqual(report["actual_youtube_calls"], 0)
        self.assertEqual(report["unauthorized_call_attempts"], 0)
        self.assertFalse(report["live_processing_completed"])
        self.assertFalse(report["human_approval"])
        self.assertNotIn("lease_token", json.dumps(report))
        self.assertNotIn("media_cache.json", json.dumps(report))

    def test_cli_validates_without_reading_private_root(self):
        with tempfile.TemporaryDirectory(prefix="p5f5_cli_") as d:
            missing = Path(d) / "should-not-be-created"
            fake = {
                "total_scenarios": 7, "passed_scenarios": 7,
                "failed_scenarios": 0, "actual_agy_calls": 0,
                "mode": "SYNTHETIC_DRYRUN_ONLY",
            }
            out = io.StringIO()
            with (patch.object(sys, "argv",
                               ["ytresearch", "--root", str(missing), "phase5f-validate"]),
                  patch("ytresearch.phase5f_validation.run_phase5f_validation_suite",
                        return_value=fake),
                  redirect_stdout(out)):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(json.loads(out.getvalue()), fake)
            self.assertFalse(missing.exists())

    def test_cli_fails_closed_on_preflight_regression(self):
        report = {"total_scenarios": 7, "passed_scenarios": 6,
                  "failed_scenarios": 1}
        with (patch.object(sys, "argv", ["ytresearch", "phase5f-validate"]),
              patch("ytresearch.phase5f_validation.run_phase5f_validation_suite",
                    return_value=report),
              redirect_stdout(io.StringIO()) as out):
            self.assertEqual(cli.main(), 2)
        self.assertEqual(json.loads(out.getvalue())["failed_scenarios"], 1)


if __name__ == "__main__":
    unittest.main()
