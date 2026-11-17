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

    def test_evaluation_rejects_modified_audio(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            (runtime / "case-000000000000" / "clip_00_30.mp3").write_bytes(b"tampered")
            report = mod.evaluate(private.parent, SESSION)
            self.assertFalse(report["trials"][0]["audio_hash_matched"])
            self.assertFalse(report["trials"][0]["pass"])


if __name__ == "__main__":
    unittest.main()
