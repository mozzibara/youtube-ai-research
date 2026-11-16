"""Synthetic privacy and correctness tests for AGY trace metadata inspection."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "tools" / "inspect_agy_read_file_trace.py"
spec = importlib.util.spec_from_file_location("agy_trace", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TraceTests(unittest.TestCase):
    def _report(self, rows, target="clip_00_30.mp3"):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "transcript.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            return mod.summarize(path, target)

    def test_three_calls_one_target_and_no_private_path(self):
        rows = [
            {"step_index": 1, "status": "DONE", "tool_calls": [
                {"name": "read_file", "arguments": {"file_path": "C:/private/clip_00_30.mp3"}}]},
            {"step_index": 3, "status": "DONE", "tool_calls": [
                {"name": "read_file", "arguments": {"file_path": "result.json"}}]},
            {"step_index": 5, "status": "DONE", "tool_calls": [
                {"name": "read_file", "arguments": {"file_path": "other.txt"}}]},
        ]
        out = self._report(rows)
        self.assertEqual(out["read_file_calls"], 3)
        self.assertEqual(out["target_file_calls"], 1)
        self.assertFalse(out["independent_audio_receipt_verified"])
        self.assertEqual(out["assessment"], "TARGET_READ_CALL_OBSERVED_AUDIO_UNVERIFIED")
        self.assertNotIn("C:/private", json.dumps(out))

    def test_audio_mime_and_data_are_hints_not_proof(self):
        rows = [{"step_index": 1, "tool_calls": [{"name": "read_file",
                 "arguments": {"path": "clip_00_30.mp3"}}],
                 "tool_results": [{"mime_type": "audio/mpeg", "data": "x" * 120}]}]
        out = self._report(rows)
        self.assertTrue(out["calls"][0]["audio_mime_hint"])
        self.assertTrue(out["calls"][0]["inline_payload_hint"])
        self.assertFalse(out["independent_audio_receipt_verified"])
        self.assertNotIn("xxx", json.dumps(out))

    def test_no_tool_calls_does_not_leak_text(self):
        out = self._report([{"step_index": 1, "text": "private content"}])
        self.assertEqual(out["assessment"], "NO_READ_FILE_CALL_OBSERVED")
        self.assertNotIn("private content", json.dumps(out))

    def test_invalid_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "transcript.jsonl"
            file.write_text("{bad\n{}", encoding="utf-8")
            self.assertEqual(mod.summarize(file)["jsonl_invalid_lines"], 1)

    def test_non_basename_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "transcript.jsonl"
            file.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                mod.summarize(file, "C:/private/clip_00_30.mp3")

    def test_schema_preview_shows_only_structure_and_no_private_values(self):
        rows = [
            {"step_index": 1, "status": "DONE",
             "tool_calls": [{"name": "read_file",
                             "arguments": {"path": "C:/SECRET_LOCATION/clip_00_30.mp3"}}]},
            {"step_index": 2, "status": "DONE",
             "content": [{"type": "text", "text": "SECRET_AUDIO_TRANSCRIPT"}],
             "tool_results": [{"mime_type": "audio/mpeg", "data": "PRIVATE_DATA" * 300}]},
            {"step_index": 3, "messages": [{"role": "tool", "content": "PRIVATE_AUDIO_CONTENT"}]}
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "transcript_full.jsonl"
            path.write_text("\\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            report = mod.summarize(path, schema_preview=True)
        self.assertEqual(report["target_file_calls"], 1)
        self.assertEqual(len(report["schema_preview"]), 3)
        p = json.dumps(report, ensure_ascii=False)
        self.assertIn("tool_results", p)
        self.assertIn("mime_type", p)
        self.assertNotIn("SECRET_LOCATION", p)
        self.assertNotIn("SECRET_AUDIO_TRANSCRIPT", p)
        self.assertNotIn("PRIVATE_DATA", p)
        self.assertNotIn("PRIVATE_AUDIO_CONTENT", p)
        self.assertFalse(report["independent_audio_receipt_verified"])

    def test_schema_preview_without_target_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "log.jsonl"
            path.write_text(json.dumps({"tool_calls": [
                {"name": "read_file", "arguments": {"path": "unrelated.json"}}]}),
                encoding="utf-8")
            report = mod.summarize(path, schema_preview=True)
        self.assertEqual(report["schema_preview"], [])

    def test_embedded_function_style_call(self):
        rows = [{"step_index": 1, "status": "DONE",
                 "tool_calls": [{"function": {
                     "name": "functions.read_file",
                     "arguments": '{"path":"clip_00_30.mp3"}'
                 }}]}]
        out = self._report(rows)
        self.assertEqual(out["target_file_calls"], 1)


if __name__ == "__main__":
    unittest.main()
