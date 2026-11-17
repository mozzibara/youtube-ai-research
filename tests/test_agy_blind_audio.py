"""Offline-only contract tests for blinded Korean SAPI / silent MP3 AGY harness."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "tools" / "agy_blind_audio.py"
spec = importlib.util.spec_from_file_location("agy_blind_audio", MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SESSION = "blind-1234567890abcdef"


class BlindAudioTests(unittest.TestCase):
    def test_randomized_balanced_plan_has_unpredictable_speech_nonces(self):
        cases = mod.plan(3, 2)
        self.assertEqual(len(cases), 5)
        self.assertEqual(sum(x["kind"] == "speech" for x in cases), 3)
        self.assertEqual(sum(x["kind"] == "silence" for x in cases), 2)
        phrases = [x["reference"] for x in cases if x["kind"] == "speech"]
        self.assertEqual(len(set(phrases)), 3)
        for x in cases:
            if x["kind"] == "speech":
                self.assertEqual(len(x["nonce"]), 6)
                self.assertTrue(set(x["nonce"]) <= set(mod.DIGITS))
                self.assertIn("확인 번호는", x["reference"])
            else:
                self.assertEqual(x["reference"], "")

    def test_speech_nonce_not_leaked_into_prompt(self):
        self.assertIn("read_file", mod.PROMPT)
        self.assertIn("NO_SPEECH", mod.PROMPT)
        self.assertNotIn("확인 번호는", mod.PROMPT)
        self.assertNotIn("파란 우산이", mod.PROMPT)
        self.assertNotIn("answer.json", mod.PROMPT)
        self.assertIn("파일을 생성하거나 수정하지 마세요", mod.PROMPT)
        self.assertIn("오직 최종 답변에", mod.PROMPT)

    def test_prepare_creates_only_media_inside_agent_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = Path(td) / "public", Path(td) / "private"
            def media_stub(output, phrase, private_dir):
                output.write_bytes(b"FAKE_TEST_MP3")
            with patch.object(mod, "_encode_mp3", side_effect=media_stub), \
                 patch.object(mod.shutil, "which", return_value="/fake/ffmpeg"):
                result = mod.prepare(runtime, private, 3, 2)
            pub = Path(result["runtime"])
            manifest = json.loads(Path(result["private_manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["cases"]), 5)
            self.assertFalse(any(pub.rglob("manifest.json")))
            self.assertFalse(any(pub.rglob("*.json")))
            self.assertTrue(all((pub / x["id"] / "clip_00_30.mp3").is_file()
                                for x in manifest["cases"]))
            self.assertEqual(sum(c["kind"] == "speech" for c in manifest["cases"]), 3)
            public_text = " ".join(p.name for p in pub.rglob("*"))
            self.assertNotIn("nonce", public_text)

    def test_roots_must_not_overlap_or_enter_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(ValueError):
                mod._ensure_roots(root, root / "secret")
        with self.assertRaises(ValueError):
            mod._ensure_roots(MODULE.parent, Path(tempfile.gettempdir()) / "separate-private")

    def test_normalized_cer_handles_spaces_punctuation_and_digit_equivalence(self):
        self.assertEqual(mod.cer("가 나 다!", "가나다"), 0)
        self.assertEqual(mod.normalize("일 이 삼 4"), "일이삼사")
        self.assertGreater(mod.cer("가나다", "마바사"), 0.9)
        self.assertEqual(mod.cer("", ""), 0)
        self.assertEqual(mod.cer("", "환각"), 1)

    def _fixture(self, base):
        runtime, private = Path(base) / "runtime", Path(base) / "truth" / SESSION
        runtime, private = runtime / SESSION, private
        runtime.mkdir(parents=True)
        private.mkdir(parents=True)
        items = []
        for index in range(5):
            cid = "case-" + f"{index:012x}"
            ws = runtime / cid
            ws.mkdir()
            audio = ws / "clip_00_30.mp3"
            audio.write_bytes(("audio" + str(index)).encode("utf-8"))
            speech = index < 3
            text = "작은 풍선이 맑은 강물 앞에서 잠시 멈췄습니다 확인 번호는 일이삼사오육 입니다" if speech else ""
            items.append({"id": cid, "kind": "speech" if speech else "silence",
                          "reference": text, "nonce": "일이삼사오육" if speech else "",
                          "sha256": hashlib.sha256(audio.read_bytes()).hexdigest()})
            output = {"verdict": "AUDIO_TRANSCRIBED" if speech else "NO_SPEECH",
                      "audio_analyzed": True,
                      "segments": [{"start": 0, "end": 15, "text": text}] if speech else []}
            (ws / "answer.json").write_text(json.dumps(output), encoding="utf-8")
            (private / (cid + ".status.json")).write_text(
                '{"submitted":true,"exit_code":0,"timed_out":false}', encoding="utf-8")
            with (private / (cid + ".ndjson")).open("w", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": "step_update", "step_update": {
                    "step_index": 1, "state": "DONE", "step_type": "tool",
                    "tool_info": {"name": "read_file",
                                  "parameters": {"Path": "clip_00_30.mp3"},
                                  "output": "metadata"}}}) + "\n")
                stream.write('{"event":"result","result":{"status":"SUCCESS"}}\n')
        (private / "manifest.json").write_text(json.dumps({
            "schema": "agy_blind_audio_v1", "session": SESSION,
            "runtime_root": str(runtime), "cases": items}), encoding="utf-8")
        return runtime, private

    def test_balanced_correct_answers_pass_behavioral_only(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._fixture(td)
            report = mod.evaluate(private.parent, SESSION)
            self.assertEqual(report["overall"], "PASS_BEHAVIORAL_AUDIO_DISCRIMINATION")
            self.assertTrue(all(x["pass"] for x in report["trials"]))
            self.assertFalse(report["independent_binary_transport_verified"])
            exported = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("작은 풍선이", exported)
            self.assertNotIn("일이삼사오육", exported)

    def test_silence_hallucination_fails(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            answer = runtime / "case-000000000003" / "answer.json"
            answer.write_text(json.dumps({
                "verdict": "AUDIO_TRANSCRIBED", "audio_analyzed": True,
                "segments": [{"start": 0, "end": 4, "text": "존재하지 않는 음성"}]}),
                encoding="utf-8")
            report = mod.evaluate(private.parent, SESSION)
            self.assertEqual(report["overall"], "FAIL_AUDIO_DISCRIMINATION")
            self.assertFalse(report["trials"][3]["pass"])

    def test_missing_tool_provenance_rejects_fake_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._fixture(td)
            trace = private / "case-000000000000.ndjson"
            trace.write_text('{"event":"result","result":{"status":"SUCCESS"}}\n',
                             encoding="utf-8")
            report = mod.evaluate(private.parent, SESSION)
            self.assertEqual(report["overall"], "FAIL_AUDIO_DISCRIMINATION")
            self.assertFalse(report["trials"][0]["mp3_read_call_observed"])

    def test_unrelated_tool_calls_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._fixture(td)
            with (private / "case-000000000000.ndjson").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": "step_update", "step_update": {
                    "step_type": "tool", "step_index": 3,
                    "tool_info": {"name": "run_command", "parameters": {"CommandLine": "echo hi"}}}}) + "\n")
            report = mod.evaluate(private.parent, SESSION)
            self.assertFalse(report["trials"][0]["pass"])
            self.assertTrue(report["trials"][0]["other_tool_calls_observed"])

    def test_incomplete_without_model_call_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            (runtime / "case-000000000001" / "answer.json").unlink()
            report = mod.evaluate(private.parent, SESSION)
            self.assertEqual(report["overall"], "INCOMPLETE")

    def test_run_dry_mode_never_invokes_agy(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._fixture(td)
            with patch.object(mod.subprocess, "run", side_effect=AssertionError("model called")):
                result = mod.run_trial(private.parent, SESSION, "case-000000000000", False)
            self.assertFalse(result["submitted"])

    def test_terminal_json_parser_only_accepts_explicit_valid_result(self):
        valid = {"verdict": "NO_SPEECH", "audio_analyzed": True, "segments": []}
        self.assertEqual(mod._parse_terminal_json_response(json.dumps(valid)), valid)
        self.assertEqual(mod._parse_terminal_json_response(
            "\x60\x60\x60json\n" + json.dumps(valid) + "\n\x60\x60\x60"), valid)
        self.assertIsNone(mod._parse_terminal_json_response(
            "This transcript is unavailable: " + json.dumps(valid)))
        self.assertIsNone(mod._parse_terminal_json_response(json.dumps({
            "verdict": "NO_SPEECH", "audio_analyzed": False, "segments": []})))
        self.assertIsNone(mod._parse_terminal_json_response(json.dumps({
            "verdict": "NO_SPEECH", "audio_analyzed": True,
            "segments": [{"start": 0, "end": 3, "text": "invented"}]})))
        self.assertIsNone(mod._parse_terminal_json_response(json.dumps({
            "verdict": "AUDIO_TRANSCRIBED", "audio_analyzed": True,
            "segments": [{"start": 0, "end": 4, "text": ""}]})))

    def test_recover_five_archived_private_final_responses_without_model(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            manifest = json.loads((private / "manifest.json").read_text(encoding="utf-8"))
            for item in manifest["cases"]:
                case_id = item["id"]
                answer = runtime / case_id / "answer.json"
                response = answer.read_text(encoding="utf-8")
                answer.unlink()
                trace = private / (case_id + ".ndjson")
                rows = trace.read_text(encoding="utf-8").splitlines()
                event = json.loads(rows[-1])
                event["result"]["response"] = response
                rows[-1] = json.dumps(event, ensure_ascii=False)
                trace.write_text("\n".join(rows) + "\n", encoding="utf-8")
            with patch.object(mod.subprocess, "run", side_effect=AssertionError("Model must not run")):
                result = mod.recover(private.parent, SESSION)
            self.assertTrue(all(x["recovery"] == "RECOVERED_FROM_TERMINAL_JSON"
                                for x in result["cases"]))
            self.assertFalse(any(runtime.rglob("answer.json")))
            self.assertEqual(len(list((private / "recoveries").glob("*.response.json"))), 5)
            evaluated = mod.evaluate(private.parent, SESSION)
            self.assertEqual(evaluated["overall"], "PASS_BEHAVIORAL_AUDIO_DISCRIMINATION")
            self.assertTrue(all(x["answer_source"] == "private_terminal_recovery"
                                for x in evaluated["trials"]))
            self.assertNotIn("작은 풍선이", json.dumps(result, ensure_ascii=False))
            again = mod.recover(private.parent, SESSION)
            self.assertTrue(all(x["recovery"] == "ALREADY_RECOVERED"
                                for x in again["cases"]))

    def test_recover_fails_closed_if_final_response_is_not_strict_json(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            cid = "case-000000000000"
            (runtime / cid / "answer.json").unlink()
            trace = private / (cid + ".ndjson")
            rows = trace.read_text(encoding="utf-8").splitlines()
            last = json.loads(rows[-1])
            last["result"]["response"] = (
                'Some commentary {"verdict":"NO_SPEECH",'
                '"audio_analyzed":true,"segments":[]}')
            rows[-1] = json.dumps(last)
            trace.write_text("\n".join(rows) + "\n", encoding="utf-8")
            result = mod.recover(private.parent, SESSION)
            row = next(x for x in result["cases"] if x["trial"] == cid)
            self.assertEqual(row["recovery"], "UNPARSEABLE_TERMINAL_JSON")
            self.assertNotIn("commentary", json.dumps(result))
            self.assertFalse((private / "recoveries" / (cid + ".response.json")).exists())

    def test_recover_requires_target_tool_and_success_event(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            cid = "case-000000000000"
            (runtime / cid / "answer.json").unlink()
            trace = private / (cid + ".ndjson")
            trace.write_text(json.dumps({"event": "result", "result": {
                "status": "SUCCESS", "response": '{"verdict":"NO_SPEECH",'
                '"audio_analyzed":true,"segments":[]}'}}) + "\n", encoding="utf-8")
            result = mod.recover(private.parent, SESSION)
            row = next(x for x in result["cases"] if x["trial"] == cid)
            self.assertEqual(row["recovery"], "NO_MP3_READ_FILE_CALL")

    def test_write_file_tool_attempt_invalidates_behavioral_result(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._fixture(td)
            cid = "case-000000000000"
            trace = private / (cid + ".ndjson")
            with trace.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": "step_update", "step_update": {
                    "step_index": 7, "step_type": "tool", "state": "DONE",
                    "tool_info": {"name": "write_file",
                                  "parameters": {"Path": "answer.json"}}}}) + "\n")
            result = mod.evaluate(private.parent, SESSION)
            self.assertFalse(result["trials"][0]["pass"])
            self.assertTrue(result["trials"][0]["other_tool_calls_observed"])
            self.assertEqual(result["overall"], "FAIL_AUDIO_DISCRIMINATION")

    def test_new_host_only_contract_rejects_model_workspace_answer(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._fixture(td)
            cid = "case-000000000000"
            status_file = private / (cid + ".status.json")
            status = json.loads(status_file.read_text(encoding="utf-8"))
            status["output_contract"] = "host_terminal_json_v2"
            status_file.write_text(json.dumps(status), encoding="utf-8")
            result = mod.evaluate(private.parent, SESSION)
            self.assertEqual(result["overall"], "FAIL_AUDIO_DISCRIMINATION")
            first = result["trials"][0]
            self.assertFalse(first["host_output_isolation"])
            self.assertEqual(first["answer_source"], "workspace_answer")

    def test_all_host_captured_terminal_json_trials_pass_offline(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            for status_file in private.glob("*.status.json"):
                status = json.loads(status_file.read_text(encoding="utf-8"))
                status["output_contract"] = "host_terminal_json_v2"
                status_file.write_text(json.dumps(status), encoding="utf-8")
            for answer in runtime.rglob("answer.json"):
                response = answer.read_text(encoding="utf-8")
                answer.unlink()
                cid = answer.parent.name
                trace = private / (cid + ".ndjson")
                rows = [json.loads(row) for row in trace.read_text(
                    encoding="utf-8").splitlines()]
                rows[-1]["result"]["response"] = response
                trace.write_text(
                    "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
                    + "\n", encoding="utf-8")
            with patch.object(mod.subprocess, "run", side_effect=AssertionError(
                    "Must never replay model requests")):
                recovery = mod.recover(private.parent, SESSION)
            self.assertEqual(
                sum(row["recovery"] == "RECOVERED_FROM_TERMINAL_JSON"
                    for row in recovery["cases"]), 5)
            scored = mod.evaluate(private.parent, SESSION)
            self.assertEqual(scored["overall"], "PASS_BEHAVIORAL_AUDIO_DISCRIMINATION")
            self.assertTrue(all(row["host_output_isolation"]
                                and row["answer_source"] == "private_terminal_recovery"
                                for row in scored["trials"]))
            self.assertFalse(scored["independent_binary_transport_verified"])

    def test_evaluation_rejects_modified_audio(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            (runtime / "case-000000000000" / "clip_00_30.mp3").write_bytes(b"tampered")
            report = mod.evaluate(private.parent, SESSION)
            self.assertFalse(report["trials"][0]["audio_hash_matched"])
            self.assertFalse(report["trials"][0]["pass"])


if __name__ == "__main__":
    unittest.main()
