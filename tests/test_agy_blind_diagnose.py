"""Synthetic-only tests for local AGY headless response trace diagnostics."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "tools" / "agy_blind_diagnose.py"
spec = importlib.util.spec_from_file_location("agy_blind_diagnose", MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
SESSION = "blind-84c9e816a9a54495"
CASE = "case-108a33b1e226"


class DiagnosisTests(unittest.TestCase):
    def _trace(self, folder: Path, rows, stderr=""):
        output = folder / "trial.ndjson"
        output.write_text("\n".join(json.dumps(row, ensure_ascii=False)
                                     for row in rows) + "\n", encoding="utf-8")
        error_file = folder / "trial.stderr"
        error_file.write_text(stderr, encoding="utf-8")
        return mod.analyze_trace(output, error_file)

    @staticmethod
    def _rows(answer_delta=None, output=None):
        rows = [
            {"event": "init", "init": {"cwd": "C:/secret/private"}},
            {"event": "step_update", "step_update": {
                "step_index": 1, "step_type": "tool", "state": "DONE",
                "tool_info": {"name": "read_file",
                              "parameters": {"Path": "C:/secret/clip_00_30.mp3"},
                              "output": output}}},
        ]
        if answer_delta is not None:
            rows.append({"event": "step_update", "step_update": {
                "step_index": 3, "step_type": "agent_response",
                "text_delta": answer_delta}})
        rows.append({"event": "result", "result": {
            "status": "SUCCESS", "response": "", "usage": {
                "input_tokens": 100, "thinking_tokens": 20}}})
        return rows

    def test_empty_success_is_not_successful_audio(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._trace(Path(td), self._rows())
        self.assertEqual(result["terminal_statuses"], ["SUCCESS"])
        self.assertEqual(result["terminal_response_character_counts"], [0])
        self.assertEqual(result["target_mp3_read_step_updates"], 1)
        self.assertEqual(result["diagnosis"], "SUCCESS_OR_OTHER_TERMINAL_WITH_NO_MODEL_TEXT")
        self.assertFalse(result["audio_bytes_reached_model_verified"])
        self.assertNotIn("C:/secret", json.dumps(result))

    def test_text_delta_detected_but_text_not_leaked(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._trace(Path(td), self._rows(answer_delta="PRIVATE_ANSWER_ABC"))
        self.assertEqual(result["diagnosis"], "EMPTY_TERMINAL_RESPONSE_AGENT_TEXT_PRESENT")
        self.assertEqual(result["agent_response_text_character_count"], len("PRIVATE_ANSWER_ABC"))
        self.assertNotIn("PRIVATE_ANSWER_ABC", json.dumps(result))

    def test_denied_tool_status_from_stderr_is_a_hint(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._trace(Path(td), self._rows(),
                                 stderr="Tool read_file permission denied for C:/secret")
        self.assertTrue(result["stderr_permission_denial_hint"])
        self.assertEqual(result["diagnosis"], "POSSIBLE_TOOL_PERMISSION_DENIAL_OR_ERROR")
        self.assertNotIn("C:/secret", json.dumps(result))

    def test_denied_tool_output_is_not_displayed(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._trace(Path(td), self._rows(output="Access denied: C:/secret"))
        self.assertTrue(result["target_tool_denial_or_error_hint"])
        self.assertTrue(result["target_tool_output_string_seen"])
        self.assertNotIn("C:/secret", json.dumps(result))

    def test_unknown_events_and_tools_are_not_emitted(self):
        rows = self._rows()
        rows.insert(2, {"event": "MY_PRIVATE_EVENT", "data": "SECRET"})
        rows.insert(3, {"event": "step_update", "step_update": {
            "step_type": "tool", "tool_info": {"name": "MY_SECRET_TOOL"}}})
        with tempfile.TemporaryDirectory() as td:
            result = self._trace(Path(td), rows)
        self.assertEqual(result["unknown_event_count"], 1)
        self.assertNotIn("MY_SECRET_TOOL", json.dumps(result))
        self.assertNotIn("MY_PRIVATE_EVENT", json.dumps(result))

    def test_headless_auto_denial_fingerprint_redacts_stderr(self):
        stderr = (
            'jetski: no output produced — a tool required the "read_file" '
            "permission that headless mode cannot prompt for, so it was "
            "auto-denied. Private file: C:/private/secret_token. "
            "Add permissions.allow in settings.json."
        )
        with tempfile.TemporaryDirectory() as td:
            result = self._trace(Path(td), self._rows(), stderr=stderr)
        self.assertTrue(result["stderr_permission_denial_hint"])
        fp = result["stderr_permission_fingerprint"]
        self.assertEqual(fp["signature"], "HEADLESS_AUTO_DENIAL")
        self.assertIn("read_file", fp["known_tools_mentioned"])
        self.assertEqual(result["diagnosis"], "POSSIBLE_TOOL_PERMISSION_DENIAL_OR_ERROR")
        as_json = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("C:/private", as_json)
        self.assertNotIn("secret_token", as_json)
        self.assertNotIn("permissions.allow", as_json)
        self.assertNotIn("jetski", as_json)

    def test_explicit_user_deny_rule_and_other_categories(self):
        fingerprint = mod.classify_stderr(
            'Permission check failed for command. Matches user-configured deny rule. '
            'C:/private/do-not-share'
        )
        self.assertEqual(fingerprint["signature"], "EXPLICIT_USER_DENY_RULE")
        self.assertIn("command", fingerprint["known_tools_mentioned"])
        self.assertNotIn("C:/private", json.dumps(fingerprint))
        sandbox = mod.classify_stderr("AppContainer denied read_file")
        self.assertEqual(sandbox["signature"], "SANDBOX_OR_APPCONTAINER_RESTRICTION")
        generic = mod.classify_stderr("Permission denied for read_file.")
        self.assertEqual(generic["signature"], "GENERIC_PERMISSION_DENIAL")
        none = mod.classify_stderr("Everything is normal.")
        self.assertEqual(none["signature"], "NO_DENIAL_PATTERN")
        self.assertEqual(none["known_tools_mentioned"], [])

    def test_unknown_log_values_and_model_payloads_are_never_printed(self):
        rows = self._rows()
        rows.insert(2, {"event": "step_update", "step_update": {
            "step_index": 2, "step_type": "agent_response",
            "content": "PRIVATE_TRANSCRIPT_TEXT_57",
            "message": "SENSITIVE_CONTENT_90",
            "x-secret-payload": "MY_SECRET_PAYLOAD"}})
        with tempfile.TemporaryDirectory() as td:
            result = self._trace(Path(td), rows, stderr="ACCESS DENIED!")
        self.assertEqual(
            result["agent_response_known_payload_key_counts"],
            {"content": 1, "message": 1}
        )
        self.assertNotIn("PRIVATE_TRANSCRIPT", json.dumps(result))
        self.assertNotIn("SENSITIVE_CONTENT", json.dumps(result))
        self.assertNotIn("MY_SECRET_PAYLOAD", json.dumps(result))
        self.assertTrue(result["stderr_permission_denial_hint"])

    def test_stderr_quota_hint_is_separate_from_permission_denial(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._trace(Path(td), self._rows(),
                                 stderr="RESOURCE_EXHAUSTED 429 quota")
        fp = result["stderr_permission_fingerprint"]
        self.assertTrue(fp["stderr_has_auth_or_quota_hint"])
        self.assertEqual(fp["signature"], "NO_DENIAL_PATTERN")
        self.assertFalse(result["stderr_permission_denial_hint"])

    def test_manifest_summary_without_publishing_ground_truth(self):
        with tempfile.TemporaryDirectory() as td:
            private = Path(td) / "truth"
            folder = private / SESSION
            folder.mkdir(parents=True)
            manifest = {
                "schema": "agy_blind_audio_v1",
                "session": SESSION,
                "cases": [{"id": CASE, "kind": "speech",
                           "reference": "비공개 정답 문장", "nonce": "팔육사삼구이"}]}
            (folder / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            for path, text in [(folder / (CASE + ".ndjson"),
                                "\n".join(json.dumps(x) for x in self._rows())),
                               (folder / (CASE + ".stderr"), "")]:
                path.write_text(text, encoding="utf-8")
            with patch.object(mod, "analyze_trace", wraps=mod.analyze_trace) as actual:
                result = mod.summarize(private, SESSION)
                self.assertEqual(actual.call_count, 1)
            self.assertFalse(result["model_called"])
            self.assertNotIn("비공개 정답", json.dumps(result, ensure_ascii=False))
            self.assertNotIn("팔육사삼구이", json.dumps(result, ensure_ascii=False))

    def test_path_traversal_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                mod.summarize(Path(td), "../.env")


if __name__ == "__main__":
    unittest.main()
