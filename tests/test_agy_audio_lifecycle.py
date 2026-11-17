"""Offline-only regression for AGY read_file event lifecycle inspection."""
from __future__ import annotations

import json
from pathlib import Path
import hashlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import agy_audio_transport_audit as audit

SESSION = "blind-fedcba9876543210"
CASE = "case-cdef01234567"
MP3 = b"ID3" + b"\\x01" * 512


class LifecycleAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.private_root = root / "truth"
        self.private = self.private_root / SESSION
        self.private.mkdir(parents=True)
        runtime = root / "runtime" / SESSION
        runtime.joinpath(CASE).mkdir(parents=True)
        runtime.joinpath(CASE, "clip_00_30.mp3").write_bytes(MP3)
        (self.private / "manifest.json").write_text(json.dumps({
            "schema": "agy_blind_audio_v2", "session": SESSION,
            "runtime_root": str(runtime),
            "cases": [{"id": CASE, "sha256": hashlib.sha256(MP3).hexdigest()}]
        }), encoding="utf-8")

    def _row(self, step_index, parameters=None, status=None, call_id=None):
        info = {"name": "read_file", "parameters": parameters or {
            "private_location": "C:/SECRET/clip_00_30.mp3",
            "private_option": 30,
        }}
        if call_id is not None:
            info["tool_call_id"] = call_id
        return {"event": "step_update", "step_update": {
            "step_type": "tool", "step_index": step_index,
            "status": status, "tool_info": info}}

    def _trace(self, *events):
        (self.private / (CASE + ".ndjson")).write_text(
            "\n".join(json.dumps(v) for v in events) + "\n",
            encoding="utf-8")

    def _report(self):
        with patch.object(audit.subprocess, "run",
                          side_effect=AssertionError("No process/model call")):
            return audit.inspect_lifecycle_session(self.private_root, SESSION)

    def _trial(self):
        return self._report()["trials"][0]

    def test_repeated_step_index_does_not_count_as_two_verified_reads(self):
        self._trace(self._row(7, status="RUNNING", call_id="opaque-id"),
                    self._row(7, status="DONE", call_id="opaque-id"),
                    {"event": "result", "result": {"status": "SUCCESS",
                      "response": "PRIVATE_TRANSCRIPT"}})
        out = self._trial()
        self.assertEqual(out["target_tool_event_count"], 2)
        self.assertEqual(out["distinct_step_index_count"], 1)
        self.assertEqual(out["repeated_step_index_groups"], 1)
        self.assertEqual(out["distinct_call_id_count"], 1)
        self.assertTrue(out["repeat_update_possible"])
        self.assertTrue(out["same_step_status_change_observed"])
        self.assertEqual(out["tool_step_statuses"], ["DONE", "RUNNING"])
        self.assertEqual(out["distinct_argument_envelope_count"], 1)
        self.assertFalse(out["verified_distinct_file_reads"])
        self.assertFalse(out["independent_binary_transport_verified"])
        self.assertNotIn("opaque-id", json.dumps(out))
        self.assertNotIn("PRIVATE_TRANSCRIPT", json.dumps(out))

    def test_unknown_private_status_does_not_leak(self):
        self._trace(self._row(7, status="PRIVATE_STATE_VALUE"))
        out = self._trial()
        self.assertEqual(out["tool_step_statuses"], ["OTHER"])
        self.assertFalse(out["same_step_status_change_observed"])
        self.assertNotIn("PRIVATE_STATE_VALUE", json.dumps(out))

    def test_same_call_id_different_steps_remains_ambiguous(self):
        self._trace(self._row(7, call_id="opaque-id"),
                    self._row(8, call_id="opaque-id"))
        t = self._trial()
        self.assertEqual(t["distinct_step_index_count"], 2)
        self.assertEqual(t["distinct_call_id_count"], 1)
        self.assertTrue(t["repeat_update_possible"])
        self.assertFalse(t["verified_distinct_file_reads"])

    def test_no_call_id_and_two_indices_do_not_prove_independent_reads(self):
        self._trace(self._row(7), self._row(8))
        t = self._trial()
        self.assertEqual(t["missing_call_id_event_count"], 2)
        self.assertEqual(t["distinct_step_index_count"], 2)
        self.assertFalse(t["verified_distinct_file_reads"])

    def test_unknown_direct_path_carrier_is_only_a_hint(self):
        self._trace(self._row(3, parameters={
            "private_path_field": "C:/SECRET/clip_00_30.mp3",
            "private_other_key": True}))
        t = self._trial()
        self.assertEqual(t["target_reference_location"], "DIRECT_VALUE")
        self.assertEqual(t["known_strict_target_count"], 0)
        self.assertEqual(t["target_tool_event_count"], 1)
        self.assertFalse(t["model_ingress_verified"])
        self.assertNotIn("private_path_field", json.dumps(t))

    def test_nested_path_carrier_cannot_become_strict_trusted_path(self):
        self._trace(self._row(3, parameters={
            "private_container": {"another_private_key": "clip_00_30.mp3"},
            "private_other_key": 42}))
        t = self._trial()
        self.assertEqual(t["target_reference_location"], "NESTED_VALUE")
        self.assertEqual(t["known_strict_target_count"], 0)
        self.assertFalse(t["independent_binary_transport_verified"])

    def test_key_only_reference_does_not_count_as_value(self):
        self._trace(self._row(3, parameters={"clip_00_30.mp3": 1,
                                             "secret_key": 2}))
        t = self._trial()
        self.assertEqual(t["target_reference_location"], "KEY_ONLY")
        self.assertEqual(t["known_strict_target_count"], 0)
        self.assertFalse(t["verified_distinct_file_reads"])

    def test_step_followup_is_adjacency_not_receipt(self):
        self._trace(self._row(4), {
            "event": "step_update", "step_update": {
                "step_type": "generic", "source": "SYSTEM",
                "content": "SECRET tool output audio/mpeg"}}, {
            "event": "result", "result": {"status": "SUCCESS",
                                         "response": "PRIVATE"}})
        t = self._trial()
        self.assertIn("STEP_UPDATE_OTHER", t["post_target_event_types"])
        self.assertIn("TERMINAL_RESULT", t["post_target_event_types"])
        self.assertEqual(t["correlation_level"], "ADJACENCY_ONLY")
        self.assertFalse(t["linked_tool_result_observed"])
        self.assertNotIn("SECRET", json.dumps(t))

    def test_explicit_correlated_tool_result_is_only_event_evidence(self):
        self._trace(self._row(3, call_id="x1"), {
            "event": "tool_result", "source": "TOOL",
            "tool_call_id": "x1", "result": {"content": "SECRET_PRIVATE"}})
        t = self._trial()
        self.assertEqual(t["correlation_level"], "EXPLICIT_TOOL_CALL_ID")
        self.assertTrue(t["linked_tool_result_observed"])
        self.assertFalse(t["model_ingress_verified"])
        self.assertNotIn("SECRET_PRIVATE", json.dumps(t))

    def test_model_supplied_result_is_not_linked(self):
        self._trace(self._row(3, call_id="x1"), {
            "event": "tool_result", "source": "MODEL",
            "tool_call_id": "x1", "result": {"content": "SECRET_PRIVATE"}})
        self.assertFalse(self._trial()["linked_tool_result_observed"])

    def test_malformed_trace_or_symlink_fails_closed(self):
        path = self.private / (CASE + ".ndjson")
        path.write_text("{BAD\n" + json.dumps(self._row(3)), encoding="utf-8")
        self.assertEqual(self._trial()["trace_status"], "INCOMPLETE_TRACE")
        path.unlink()
        other = self.private / "other.ndjson"
        other.write_text(json.dumps(self._row(3)), encoding="utf-8")
        try:
            path.symlink_to(other)
        except OSError:
            self.skipTest("No symlink privileges")
        self.assertEqual(self._trial()["trace_status"], "UNSAFE_TRACE")

    def test_read_only_private_path_and_cli(self):
        self._trace(self._row(5), self._row(5))
        before = {str(p): p.read_bytes() for p in self.private.rglob("*")
                  if p.is_file()}
        result = self._report()
        after = {str(p): p.read_bytes() for p in self.private.rglob("*")
                 if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(result["model_called"])
        dumped = json.dumps(result)
        for forbidden in ("PRIVATE_", "C:/SECRET", str(self.private), "clip_00_30.mp3"):
            self.assertNotIn(forbidden, dumped)
        tool = Path(__file__).resolve().parents[1] / "tools" / "agy_audio_transport_audit.py"
        proc = subprocess.run(
            [sys.executable, str(tool), "--inspect-lifecycle",
             "--session", SESSION, "--private-root", str(self.private_root)],
            cwd=tool.parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["schema"],
                         "agy_cli_audio_lifecycle_v1")


if __name__ == "__main__":
    unittest.main()
