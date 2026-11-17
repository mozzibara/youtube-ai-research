"""Offline byte-level AGY tool-result audit: synthetic fixtures only, no model calls."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import agy_audio_transport_audit as audit


SESSION = "blind-0123456789abcdef"
CASE = "case-123456789abc"
MP3 = b"ID3" + bytes(range(256)) * 3


class AudioByteReceiptAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.private_root = root / "private"
        self.private = self.private_root / SESSION
        self.private.mkdir(parents=True)
        self.runtime = root / "runtime" / SESSION
        self.case = self.runtime / CASE
        self.case.mkdir(parents=True)
        (self.case / "clip_00_30.mp3").write_bytes(MP3)
        (self.private / "manifest.json").write_text(json.dumps({
            "schema": "agy_blind_audio_v2",
            "session": SESSION,
            "runtime_root": str(self.runtime),
            "cases": [{"id": CASE, "sha256": hashlib.sha256(MP3).hexdigest(),
                       "kind": "speech", "reference": "PRIVATE_PHRASE",
                       "nonce": "PRIVATE_NONCE"}]
        }), encoding="utf-8")

    def _call(self, *, result=None, name="read_file", target="clip_00_30.mp3",
              call_id="tool-123"):
        info = {"name": name, "parameters": {"file_path": target},
                "tool_call_id": call_id}
        if result is not None:
            info["result"] = result
        return {"event": "step_update", "step_update": {
            "step_type": "tool", "tool_info": info}}

    def _inline(self, data=MP3, mime="audio/mpeg"):
        return {"parts": [{"inline_data": {
            "mime_type": mime, "data": base64.b64encode(data).decode("ascii")}}]}

    def _trace(self, *rows):
        (self.private / (CASE + ".ndjson")).write_text(
            "\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")

    def _check(self):
        with patch.object(audit.subprocess, "run",
                          side_effect=AssertionError("No process execution")):
            return audit.audit_session(self.private_root, SESSION)

    def _trial(self):
        return self._check()["trials"][0]

    def test_exact_base64_in_typed_tool_result_is_local_byte_match_only(self):
        self._trace(self._call(result=self._inline()))
        trial = self._trial()
        self.assertEqual(trial["assessment"], "MATCHED_CLI_TOOL_RESULT_BYTES")
        self.assertTrue(trial["exact_mp3_bytes_in_tool_result"])
        self.assertTrue(trial["target_read_call_observed"])
        self.assertFalse(trial["independent_binary_transport_verified"])
        self.assertFalse(trial["model_ingress_verified"])
        self.assertNotIn("PRIVATE_", json.dumps(self._check()))

    def test_separate_correlated_tool_result_supported(self):
        self._trace(self._call(), {"event": "tool_result", "source": "TOOL",
                    "tool_call_id": "tool-123", "result": self._inline()})
        self.assertEqual(self._trial()["assessment"], "MATCHED_CLI_TOOL_RESULT_BYTES")

    def test_unlinked_or_model_authored_blob_never_passes(self):
        self._trace(self._call(), {"event": "tool_result", "source": "MODEL",
                    "tool_call_id": "tool-123", "result": self._inline()},
                    {"event": "result", "result": {"response": self._inline()}})
        self.assertEqual(self._trial()["assessment"], "NO_TOOL_RESULT_AUDIO_BYTES")
        self._trace(self._call(), {"event": "tool_result", "source": "TOOL",
                    "tool_call_id": "wrong", "result": self._inline()})
        self.assertEqual(self._trial()["assessment"], "NO_TOOL_RESULT_AUDIO_BYTES")

    def test_path_metadata_and_call_only_are_not_bytes(self):
        self._trace(self._call(result={"path": "clip_00_30.mp3",
                                      "mime_type": "audio/mpeg"}))
        self.assertEqual(self._trial()["assessment"], "NO_TOOL_RESULT_AUDIO_BYTES")

    def test_payload_in_call_arguments_is_not_tool_result(self):
        row = self._call()
        row["step_update"]["tool_info"]["parameters"]["inline_data"] = self._inline()
        self._trace(row)
        self.assertEqual(self._trial()["assessment"], "NO_TOOL_RESULT_AUDIO_BYTES")

    def test_tampering_invalid_base64_or_wrong_mime_does_not_match(self):
        self._trace(self._call(result=self._inline(MP3 + b"X")))
        self.assertEqual(self._trial()["assessment"], "CLI_TOOL_RESULT_BYTES_MISMATCH")
        self._trace(self._call(result=self._inline(mime="application/json")))
        self.assertEqual(self._trial()["assessment"], "NO_TOOL_RESULT_AUDIO_BYTES")
        self._trace(self._call(result={"inline_data": {
            "mime_type": "audio/mpeg", "data": "@@@@"}}))
        self.assertEqual(self._trial()["assessment"], "INVALID_TOOL_RESULT_ENCODING")

    def test_no_read_call_and_missing_trace_are_unknown_not_failed_delivery(self):
        self._trace(self._call(name="read_file", target="other.mp3",
                               result=self._inline()))
        self.assertEqual(self._trial()["assessment"], "NO_TARGET_READ_CALL")
        (self.private / (CASE + ".ndjson")).unlink()
        self.assertEqual(self._trial()["assessment"], "TRACE_MISSING")

    def test_manifest_hash_mismatch_precedes_trace_claim(self):
        self._trace(self._call(result=self._inline()))
        (self.case / "clip_00_30.mp3").write_bytes(b"changed")
        self.assertEqual(self._trial()["assessment"], "MEDIA_HASH_MISMATCH")

    def test_symlinked_trace_rejected(self):
        target = self.private / "untrusted.ndjson"
        target.write_text(json.dumps(self._call(result=self._inline())),
                          encoding="utf-8")
        trace = self.private / (CASE + ".ndjson")
        try:
            trace.symlink_to(target)
        except OSError:
            self.skipTest("symlink unavailable")
        self.assertEqual(self._trial()["assessment"], "UNSAFE_TRACE")

    def test_invalid_json_is_incomplete(self):
        trace = self.private / (CASE + ".ndjson")
        trace.write_text("{invalid\n" + json.dumps(self._call()),
                         encoding="utf-8")
        self.assertEqual(self._trial()["assessment"], "INCOMPLETE_TRACE")

    def test_privacy_and_read_only_no_new_files(self):
        self._trace(self._call(result={"parts": [{"inline_data": {
            "mime_type": "audio/mpeg", "data": base64.b64encode(MP3).decode(),
            "private_secret": "DO_NOT_EXPOSE"}}]}))
        before = {str(p): p.read_bytes() for p in self.private.rglob("*") if p.is_file()}
        output = self._check()
        after = {str(p): p.read_bytes() for p in self.private.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        dumped = json.dumps(output)
        for private in ("DO_NOT_EXPOSE", "PRIVATE_PHRASE", "PRIVATE_NONCE",
                        base64.b64encode(MP3).decode(), str(self.private)):
            self.assertNotIn(private, dumped)

    def test_malformed_tool_name_is_ignored_without_crash(self):
        row = self._call()
        row["step_update"]["tool_info"]["name"] = {"injected": "PRIVATE_VALUE"}
        self._trace(row)
        out = self._trial()
        self.assertEqual(out["assessment"], "NO_TARGET_READ_CALL")
        self.assertNotIn("PRIVATE_VALUE", json.dumps(out))

    def test_real_cli_name_at_step_level_is_not_lost(self):
        """Earlier blind evaluator accepts step.tool_name when info.name absent."""
        row = self._call(result=self._inline())
        info = row["step_update"]["tool_info"]
        row["step_update"]["tool_name"] = info.pop("name")
        self._trace(row)
        trial = self._trial()
        self.assertTrue(trial["target_read_call_observed"])
        self.assertEqual(trial["assessment"], "MATCHED_CLI_TOOL_RESULT_BYTES")
        self.assertFalse(trial["model_ingress_verified"])

    def test_parameters_are_json_string_in_event(self):
        row = self._call()
        info = row["step_update"]["tool_info"]
        info["parameters"] = json.dumps({"file_path": "C:/hidden/clip_00_30.mp3"})
        self._trace(row)
        self.assertTrue(self._trial()["target_read_call_observed"])
        self.assertEqual(self._trial()["assessment"], "NO_TOOL_RESULT_AUDIO_BYTES")

    def test_existing_provenance_recognized_but_byte_parser_unsupported(self):
        """Old read_file inspector sees this; missing strict path must not say no read."""
        row = self._call()
        row["step_update"]["tool_info"]["parameters"] = {
            "nested_cli_parameters": {"input_file": "D:/secret/clip_00_30.mp3"}
        }
        self._trace(row)
        trial = self._trial()
        self.assertEqual(trial["assessment"], "TARGET_READ_CALL_SHAPE_UNSUPPORTED")
        self.assertTrue(trial["target_read_call_observed"])
        self.assertFalse(trial["exact_mp3_bytes_in_tool_result"])
        self.assertTrue(trial["legacy_provenance_only"])

    def test_model_text_is_not_provenance_fallback(self):
        self._trace({"event": "result", "result": {
            "response": "I read clip_00_30.mp3", "status": "SUCCESS"}})
        trial = self._trial()
        self.assertEqual(trial["assessment"], "NO_TARGET_READ_CALL")
        self.assertFalse(trial["target_read_call_observed"])

    def test_no_byte_match_in_unsupported_cli_shape_even_if_blob_present(self):
        row = self._call(result=self._inline())
        info = row["step_update"]["tool_info"]
        info["parameters"] = {"nested_cli_parameters": {
            "input_file": "D:/secret/clip_00_30.mp3"
        }}
        self._trace(row)
        trial = self._trial()
        self.assertEqual(trial["assessment"], "TARGET_READ_CALL_SHAPE_UNSUPPORTED")
        self.assertFalse(trial["exact_mp3_bytes_in_tool_result"])
        self.assertEqual(trial["tool_result_audio_payload_count"], 0)

    def test_invalid_session_refused(self):
        with self.assertRaises(ValueError):
            audit.audit_session(self.private_root, "../unsafe")

    def test_data_url_mp3_supported_without_raw_export(self):
        media = self._inline()
        payload = media["parts"][0]["inline_data"]
        payload["data"] = "data:audio/mpeg;base64," + payload["data"]
        self._trace(self._call(result=media))
        self.assertEqual(self._trial()["assessment"], "MATCHED_CLI_TOOL_RESULT_BYTES")


if __name__ == "__main__":
    unittest.main()
