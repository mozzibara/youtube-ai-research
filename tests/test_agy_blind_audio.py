"""Offline-only contract tests for blinded Korean SAPI / silent MP3 AGY harness."""
import hashlib
import importlib.util
import json
from array import array
from pathlib import Path
import wave
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
                result = mod.prepare(runtime, private, 3, 2, tone_count=0)
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

    def test_new_three_condition_plan_balanced_and_secret_tone_count(self):
        cases = mod.plan(speech_count=3, silence_count=1, tone_count=1)
        self.assertEqual(sorted(x["kind"] for x in cases),
                         ["silence", "speech", "speech", "speech", "tone"])
        tone = next(x for x in cases if x["kind"] == "tone")
        self.assertIn(tone["beep_count"], mod.TONE_COUNTS)
        self.assertEqual(tone["reference"], "")
        self.assertNotIn(f'"beep_count":{tone["beep_count"]}', mod.PROMPT)
        self.assertIn("acoustic_class", mod.PROMPT)
        self.assertIn("신호음", mod.PROMPT)
        for invalid in (-1, 11):
            with self.assertRaises(ValueError):
                mod.plan(3, 1, invalid)

    def test_synthetic_wav_has_exact_random_beep_count_and_30s_duration(self):
        with tempfile.TemporaryDirectory() as td:
            for count in (2, 3, 5):
                path = Path(td) / f"tone{count}.wav"
                with patch.object(mod.subprocess, "run",
                                  side_effect=AssertionError("No external process")):
                    mod._synthesize_tone_wav(path, count)
                with wave.open(str(path), "rb") as audio:
                    self.assertEqual(audio.getframerate(), 16000)
                    self.assertEqual(audio.getnframes(), 480000)
                    self.assertEqual(audio.getnchannels(), 1)
                    self.assertEqual(audio.getsampwidth(), 2)
                    samples = array("h")
                    samples.frombytes(audio.readframes(480000))
                # Count contiguous 100 ms windows with audible tone. This
                # validates waveform, not only a synthetic manifest field.
                chunks = [
                    max(map(abs, samples[i:i + 1600])) > 2000
                    for i in range(0, 480000, 1600)
                ]
                audible_starts = sum(active and (i == 0 or not chunks[i - 1])
                                     for i, active in enumerate(chunks))
                self.assertEqual(audible_starts, count)
                self.assertTrue(all(x == 0 for x in samples[16000 * 8:]))
            with self.assertRaises(ValueError):
                mod._synthesize_tone_wav(Path(td) / "bad.wav", 0)
            with self.assertRaises(ValueError):
                mod._synthesize_tone_wav(Path(td) / "bad.wav", True)

    def test_prepare_three_condition_workspace_does_not_reveal_class_or_beeps(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, truth = Path(td) / "media", Path(td) / "truth"
            def fake_encoder(out, phrase, private_dir, **kwargs):
                out.write_bytes(b"FAKE_MP3")
            with patch.object(mod, "_encode_mp3", side_effect=fake_encoder), \
                 patch.object(mod.shutil, "which", return_value="/fake/tool"), \
                 patch.object(mod.subprocess, "run",
                              side_effect=AssertionError("Must not call model")):
                result = mod.prepare(runtime, truth, 1, 1, 1)
            manifest = json.loads(Path(result["private_manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "agy_blind_audio_v2")
            self.assertEqual(len(manifest["cases"]), 3)
            pub = Path(result["runtime"])
            self.assertEqual(len(list(pub.rglob("clip_00_30.mp3"))), 3)
            self.assertFalse(any(pub.rglob("*.json")))
            tone = next(x for x in manifest["cases"] if x["kind"] == "tone")
            self.assertIn(tone["beep_count"], mod.TONE_COUNTS)
            for case in manifest["cases"]:
                workspace = pub / case["id"]
                self.assertEqual([x.name for x in workspace.iterdir()],
                                 ["clip_00_30.mp3"])
                self.assertNotIn(case["kind"], workspace.name)

    def _three_condition_fixture(self, td):
        runtime, private = self._fixture(td)
        manifest_file = private / "manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["schema"] = "agy_blind_audio_v2"
        manifest["cases"][3]["kind"] = "tone"
        manifest["cases"][3]["beep_count"] = 3
        for item in manifest["cases"]:
            item.setdefault("beep_count", 0)
            case_id = item["id"]
            original = runtime / case_id / "answer.json"
            answer = json.loads(original.read_text(encoding="utf-8"))
            answer["acoustic_class"] = item["kind"].upper()
            answer["beep_count"] = item["beep_count"]
            # Tone and silence: no speech segments, regardless of event.
            original.unlink()
            status_path = private / (case_id + ".status.json")
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["output_contract"] = mod.ACOUSTIC_CONTRACT
            status_path.write_text(json.dumps(status), encoding="utf-8")
            trace_path = private / (case_id + ".ndjson")
            rows = [json.loads(line) for line in trace_path.read_text(
                encoding="utf-8").splitlines()]
            rows[-1]["result"]["response"] = json.dumps(answer, ensure_ascii=False)
            trace_path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
                + "\n", encoding="utf-8")
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False),
                                 encoding="utf-8")
        return runtime, private

    def test_three_condition_host_captured_answers_pass_with_correct_counts(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._three_condition_fixture(td)
            with patch.object(mod.subprocess, "run",
                              side_effect=AssertionError("No model requests")):
                recovery = mod.recover(private.parent, SESSION)
            self.assertEqual(
                sum(x["recovery"] == "RECOVERED_FROM_TERMINAL_JSON"
                    for x in recovery["cases"]), 5)
            report = mod.evaluate(private.parent, SESSION)
            self.assertEqual(report["overall"], "PASS_BEHAVIORAL_AUDIO_DISCRIMINATION")
            self.assertEqual(report["tone_trials"], 1)
            self.assertEqual(report["silence_trials"], 1)
            self.assertTrue(all(x["pass"] and x["acoustic_class_match"]
                                for x in report["trials"]))
            tone = next(x for x in report["trials"] if x["kind"] == "tone")
            self.assertTrue(tone["tone_count_match"])
            self.assertFalse(report["independent_binary_transport_verified"])
            self.assertFalse(any(runtime.rglob("answer.json")))
            self.assertNotIn("작은 풍선이", json.dumps(report, ensure_ascii=False))

    def test_tone_claimed_silence_or_wrong_count_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._three_condition_fixture(td)
            mod.recover(private.parent, SESSION)
            tone = private / "recoveries" / "case-000000000003.response.json"
            payload = json.loads(tone.read_text(encoding="utf-8"))
            payload["beep_count"] = 2
            tone.write_text(json.dumps(payload), encoding="utf-8")
            report = mod.evaluate(private.parent, SESSION)
            self.assertEqual(report["overall"], "FAIL_AUDIO_DISCRIMINATION")
            self.assertFalse(report["trials"][3]["tone_count_match"])
            self.assertFalse(report["trials"][3]["pass"])
            payload["acoustic_class"] = "SILENCE"
            payload["beep_count"] = 0
            tone.write_text(json.dumps(payload), encoding="utf-8")
            report = mod.evaluate(private.parent, SESSION)
            self.assertFalse(report["trials"][3]["acoustic_class_match"])
            self.assertFalse(report["trials"][3]["pass"])

    def test_three_way_insufficient_sample_is_provisional_not_full_pass(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._three_condition_fixture(td)
            manifest_file = private / "manifest.json"
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest["cases"] = [manifest["cases"][0], manifest["cases"][3],
                                 manifest["cases"][4]]
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
            mod.recover(private.parent, SESSION)
            scored = mod.evaluate(private.parent, SESSION)
            self.assertEqual(scored["overall"], "PROVISIONAL_INSUFFICIENT_TRIALS")
            self.assertEqual((scored["speech_trials"], scored["tone_trials"],
                              scored["silence_trials"]), (1, 1, 1))

    def test_v3_parser_fail_closed_on_inconsistent_sound_class_or_count(self):
        base = {"verdict": "NO_SPEECH", "audio_analyzed": True,
                "segments": [], "acoustic_class": "TONE", "beep_count": 3}
        self.assertIsNotNone(mod._parse_terminal_json_response(
            json.dumps(base), contract=mod.ACOUSTIC_CONTRACT))
        for bad in ({**base, "beep_count": True},
                    {**base, "beep_count": 0},
                    {**base, "acoustic_class": "SILENCE", "beep_count": 3},
                    {**base, "acoustic_class": {"private": "secret"}},
                    {**base, "verdict": "NO_AUDIO_ACCESS"},
                    {**base, "audio_analyzed": False}):
            self.assertIsNone(mod._parse_terminal_json_response(
                json.dumps(bad), contract=mod.ACOUSTIC_CONTRACT))
        no_access = {"verdict": "NO_AUDIO_ACCESS", "audio_analyzed": False,
                     "segments": [], "acoustic_class": "UNAVAILABLE", "beep_count": None}
        self.assertIsNotNone(mod._parse_terminal_json_response(
            json.dumps(no_access), contract=mod.ACOUSTIC_CONTRACT))

    def test_terminal_response_validation_reasons_are_safe_and_specific(self):
        base = {"verdict": "NO_SPEECH", "audio_analyzed": True,
                "acoustic_class": "SILENCE", "beep_count": 0, "segments": []}
        cases = [
            (json.dumps(base), "VALID_TERMINAL_JSON"),
            ("utterance " + json.dumps(base), "INVALID_JSON_SYNTAX_OR_ADDITIONAL_TEXT"),
            ("{" + "invalid", "INVALID_JSON_SYNTAX_OR_ADDITIONAL_TEXT"),
            (json.dumps([base]), "JSON_TOP_LEVEL_NOT_OBJECT"),
            (json.dumps({**base, "verdict": "NOT_A_VERDICT"}),
             "VERDICT_MISSING_OR_INVALID"),
            (json.dumps({**base, "audio_analyzed": "yes"}),
             "AUDIO_ANALYZED_NOT_BOOLEAN"),
            (json.dumps({**base, "segments": "private-phrase"}),
             "SEGMENTS_NOT_VALID_LIST"),
            (json.dumps({**base, "segments": [{"text": "private-phrase"}]}),
             "SEGMENT_ELEMENT_SCHEMA_INVALID"),
            (json.dumps({**base, "audio_analyzed": False}),
             "VERDICT_SEGMENTS_OR_ANALYSIS_CONFLICT"),
            (json.dumps({**base, "acoustic_class": "NOT_AN_AUDIO_CLASS"}),
             "ACOUSTIC_CLASS_MISSING_OR_INVALID"),
            (json.dumps({**base, "beep_count": None}),
             "ACOUSTIC_VERDICT_OR_BEEP_COUNT_CONFLICT"),
            (json.dumps({**base, "acoustic_class": "TONE", "beep_count": 3}),
             "VALID_TERMINAL_JSON"),
            (json.dumps({**base, "verdict": "NO_AUDIO_ACCESS",
                         "audio_analyzed": False}),
             "ACOUSTIC_VERDICT_OR_BEEP_COUNT_CONFLICT"),
            (json.dumps({**base, "verdict": "NO_AUDIO_ACCESS",
                         "audio_analyzed": False,
                         "acoustic_class": "UNAVAILABLE",
                         "beep_count": None}),
             "VALID_TERMINAL_JSON"),
        ]
        for raw, expected in cases:
            with self.subTest(category=expected):
                diag = mod._categorize_response_validation(
                    raw, mod.ACOUSTIC_CONTRACT)
                self.assertEqual(diag["validation_category"], expected)
                serialized = json.dumps(diag, ensure_ascii=False)
                for forbidden in ["private-phrase", "NOT_A_VERDICT",
                                  "NOT_AN_AUDIO_CLASS", "utterance"]:
                    self.assertNotIn(forbidden, serialized)
                self.assertFalse(diag["independent_audio_receipt_verified"])

    def test_terminal_diagnostic_reads_existing_private_logs_without_model(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._three_condition_fixture(td)
            case_id = "case-000000000004"
            target = private / (case_id + ".ndjson")
            lines = [json.loads(x) for x in target.read_text(
                encoding="utf-8").splitlines()]
            # An invalid silence answer. Preserve it; never silently
            # relabel NO_AUDIO_ACCESS as a successful silent detection.
            lines[-1]["result"]["response"] = json.dumps({
                "verdict": "NO_AUDIO_ACCESS", "audio_analyzed": False,
                "acoustic_class": "SILENCE", "beep_count": 0,
                "segments": [], "private_secret": "NEVER_EXPORT_THIS"
            })
            target.write_text("\n".join(json.dumps(x) for x in lines) + "\n",
                              encoding="utf-8")
            before = target.read_bytes()
            with patch.object(mod.subprocess, "run",
                              side_effect=AssertionError("No AGY replay")), \
                 patch.object(mod, "_json",
                              side_effect=AssertionError("Read only")):
                report = mod.diagnose_terminal_responses(private.parent, SESSION)
            self.assertEqual(before, target.read_bytes())
            self.assertFalse(report["model_called"])
            self.assertTrue(report["all_responses_private"])
            self.assertEqual(len(report["trials"]), 5)
            item = next(x for x in report["trials"] if x["trial"] == case_id)
            self.assertEqual(item["validation_category"],
                             "ACOUSTIC_VERDICT_OR_BEEP_COUNT_CONFLICT")
            self.assertEqual(item["reported_verdict"], "NO_AUDIO_ACCESS")
            self.assertEqual(item["reported_acoustic_class"], "SILENCE")
            self.assertEqual(item["reported_beep_count_type"], "INTEGER")
            self.assertNotIn("NEVER_EXPORT_THIS", json.dumps(report))

    def test_terminal_diagnostic_missing_log_or_invalid_status_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._three_condition_fixture(td)
            case_id = "case-000000000004"
            (private / (case_id + ".ndjson")).unlink()
            status_path = private / (case_id + ".status.json")
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["output_contract"] = {"secret": "DO_NOT_EXPORT"}
            status_path.write_text(json.dumps(status), encoding="utf-8")
            report = mod.diagnose_terminal_responses(private.parent, SESSION)
            item = next(x for x in report["trials"] if x["trial"] == case_id)
            self.assertEqual(item["validation_category"], "TRACE_MISSING")
            self.assertEqual(item["output_contract"], "UNKNOWN_CONTRACT")
            self.assertNotIn("DO_NOT_EXPORT", json.dumps(report))

    def test_valid_silence_tone_claim_is_confirmed_acoustic_failure(self):
        """Strict-valid TONE JSON on SILENCE must be a confirmed FAIL."""
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._three_condition_fixture(td)
            recovery = mod.recover(private.parent, SESSION)
            self.assertTrue(all(x["recovery"] == "RECOVERED_FROM_TERMINAL_JSON"
                                for x in recovery["cases"]))
            silence_id = "case-000000000004"
            path = private / "recoveries" / (silence_id + ".response.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["acoustic_class"] = "TONE"
            payload["beep_count"] = 3
            self.assertIsNotNone(mod._parse_terminal_json_response(
                json.dumps(payload), contract=mod.ACOUSTIC_CONTRACT))
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(mod.subprocess, "run",
                              side_effect=AssertionError("No model calls")):
                scored = mod.evaluate(private.parent, SESSION)
            self.assertTrue(scored["all_complete"])
            self.assertTrue(scored["confirmed_acoustic_misclassification"])
            self.assertEqual(scored["overall"], "FAIL_AUDIO_DISCRIMINATION")
            case = next(x for x in scored["trials"] if x["trial"] == silence_id)
            self.assertEqual(case["verdict"], "NO_SPEECH")
            self.assertEqual(case["acoustic_class_reported"], "TONE")
            self.assertFalse(case["acoustic_class_match"])
            self.assertTrue(case["observed_acoustic_misclassification"])
            self.assertEqual(case["acoustic_misclassification_evidence_source"],
                             "VALIDATED_PRIVATE_TERMINAL_RESPONSE")
            self.assertEqual(case["trial_outcome"],
                             "FAIL_OBSERVED_ACOUSTIC_MISCLASSIFICATION")
            self.assertFalse(case["pass"])
            self.assertFalse(any(runtime.rglob("answer.json")))

    def test_valid_silence_misclassification_overrides_unrecovered_tone(self):
        """Exact real 3+1+1 shape: 3 speech PASS, 1 tone missing, silence TONE."""
        with tempfile.TemporaryDirectory() as td:
            _, private = self._three_condition_fixture(td)
            mod.recover(private.parent, SESSION)
            silence_id = "case-000000000004"
            tone_id = "case-000000000003"
            path = private / "recoveries" / (silence_id + ".response.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.update({"acoustic_class": "TONE", "beep_count": 3})
            path.write_text(json.dumps(payload), encoding="utf-8")
            (private / "recoveries" / (tone_id + ".response.json")).unlink()
            score = mod.evaluate(private.parent, SESSION)
            self.assertFalse(score["all_complete"])
            self.assertEqual(score["overall"], "FAIL_AUDIO_DISCRIMINATION")
            self.assertTrue(score["confirmed_acoustic_misclassification"])
            self.assertEqual(sum(x["pass"] for x in score["trials"]), 3)
            tone = next(x for x in score["trials"] if x["trial"] == tone_id)
            self.assertEqual(tone["trial_outcome"],
                             "INCOMPLETE_UNRECOVERED_TERMINAL_RESPONSE")
            silent = next(x for x in score["trials"] if x["trial"] == silence_id)
            self.assertEqual(silent["trial_outcome"],
                             "FAIL_OBSERVED_ACOUSTIC_MISCLASSIFICATION")

    def test_valid_wrong_class_without_read_file_provenance_not_confirmed(self):
        with tempfile.TemporaryDirectory() as td:
            _, private = self._three_condition_fixture(td)
            mod.recover(private.parent, SESSION)
            cid = "case-000000000004"
            path = private / "recoveries" / (cid + ".response.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.update({"acoustic_class": "TONE", "beep_count": 3})
            path.write_text(json.dumps(payload), encoding="utf-8")
            trace = private / (cid + ".ndjson")
            rows = [json.loads(x) for x in trace.read_text(
                encoding="utf-8").splitlines()]
            trace.write_text("\n".join(json.dumps(x) for x in rows
                               if x.get("event") != "step_update") + "\n",
                             encoding="utf-8")
            score = mod.evaluate(private.parent, SESSION)
            self.assertFalse(score["confirmed_acoustic_misclassification"])
            self.assertEqual(score["overall"], "FAIL_AUDIO_DISCRIMINATION")
            case = next(x for x in score["trials"] if x["trial"] == cid)
            self.assertFalse(case["observed_acoustic_misclassification"])
            self.assertIsNone(case["acoustic_misclassification_evidence_source"])

    def test_invalid_silence_tone_claim_is_decisive_failure_not_missing(self):
        """Match observed real case: NO_SPEECH/TONE/int=0 for a silent MP3."""
        with tempfile.TemporaryDirectory() as td:
            _, private = self._three_condition_fixture(td)
            case_id = "case-000000000004"
            trace = private / (case_id + ".ndjson")
            events = [json.loads(x) for x in trace.read_text(
                encoding="utf-8").splitlines()]
            events[-1]["result"]["response"] = json.dumps({
                "verdict": "NO_SPEECH",
                "acoustic_class": "TONE",
                "audio_analyzed": True,
                "beep_count": 0,
                "segments": [],
                "private_unrecognized": "DO_NOT_PRINT_PRIVATE_CONTENT"
            })
            trace.write_text("\n".join(json.dumps(x) for x in events) + "\n",
                             encoding="utf-8")
            original_bytes = trace.read_bytes()
            with patch.object(mod.subprocess, "run",
                              side_effect=AssertionError("Must not replay AGY")):
                recovered = mod.recover(private.parent, SESSION)
            silent = next(x for x in recovered["cases"] if x["trial"] == case_id)
            self.assertEqual(silent["recovery"], "UNPARSEABLE_TERMINAL_JSON")
            self.assertFalse((private / "recoveries" /
                              (case_id + ".response.json")).exists())
            report = mod.evaluate(private.parent, SESSION)
            self.assertFalse(report["all_complete"])
            self.assertEqual(report["overall"], "FAIL_AUDIO_DISCRIMINATION")
            self.assertTrue(report["confirmed_acoustic_misclassification"])
            case = next(x for x in report["trials"] if x["trial"] == case_id)
            self.assertFalse(case["pass"])
            self.assertEqual(case["verdict"], "MISSING")
            self.assertEqual(case["observed_rejected_acoustic_class"], "TONE")
            self.assertEqual(case["rejected_terminal_validation_category"],
                             "ACOUSTIC_VERDICT_OR_BEEP_COUNT_CONFLICT")
            self.assertTrue(case["observed_acoustic_misclassification"])
            self.assertEqual(case["trial_outcome"],
                             "FAIL_OBSERVED_ACOUSTIC_MISCLASSIFICATION")
            self.assertEqual(original_bytes, trace.read_bytes())
            self.assertNotIn("DO_NOT_PRINT_PRIVATE_CONTENT",
                             json.dumps(report, ensure_ascii=False))

    def test_ambiguous_or_absent_output_stays_incomplete_not_auto_failed(self):
        for raw in ["garbled terminal text",
                    json.dumps({"verdict": "NO_SPEECH",
                                "acoustic_class": "INVALID_PRIVATE_LABEL",
                                "audio_analyzed": True,
                                "beep_count": 0, "segments": []}),
                    ""]:
            with self.subTest(response_length=len(raw)):
                with tempfile.TemporaryDirectory() as td:
                    _, private = self._three_condition_fixture(td)
                    case_id = "case-000000000004"
                    trace = private / (case_id + ".ndjson")
                    rows = [json.loads(x) for x in trace.read_text(
                        encoding="utf-8").splitlines()]
                    rows[-1]["result"]["response"] = raw
                    trace.write_text(
                        "\n".join(json.dumps(x) for x in rows) + "\n",
                        encoding="utf-8")
                    report = mod.evaluate(private.parent, SESSION)
                    self.assertEqual(report["overall"], "INCOMPLETE")
                    self.assertFalse(report["confirmed_acoustic_misclassification"])
                    case = next(x for x in report["trials"] if x["trial"] == case_id)
                    self.assertEqual(case["verdict"], "MISSING")
                    self.assertFalse(case["observed_acoustic_misclassification"])
                    self.assertEqual(case["trial_outcome"],
                                     "INCOMPLETE_UNRECOVERED_TERMINAL_RESPONSE")
                    self.assertNotIn("INVALID_PRIVATE_LABEL", json.dumps(report))

    def test_wrong_class_without_provenance_cannot_be_confirmed(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._three_condition_fixture(td)
            cid = "case-000000000004"
            trace = private / (cid + ".ndjson")
            rows = [json.loads(x) for x in trace.read_text(
                encoding="utf-8").splitlines()]
            rows[-1]["result"]["response"] = json.dumps({
                "verdict": "NO_SPEECH",
                "acoustic_class": "TONE",
                "audio_analyzed": True,
                "beep_count": 0, "segments": []})
            # Drop the read_file provenance: no confident attribution.
            rows = [x for x in rows if x.get("event") != "step_update"]
            trace.write_text("\n".join(json.dumps(x) for x in rows) + "\n",
                             encoding="utf-8")
            report = mod.evaluate(private.parent, SESSION)
            self.assertEqual(report["overall"], "INCOMPLETE")
            self.assertFalse(report["confirmed_acoustic_misclassification"])

    def test_speech_missing_answer_has_no_artificial_cer_of_one(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            (runtime / "case-000000000000" / "answer.json").unlink()
            report = mod.evaluate(private.parent, SESSION)
            case = report["trials"][0]
            self.assertEqual(report["overall"], "INCOMPLETE")
            self.assertIsNone(case["cer"])
            self.assertIsNone(case["nonce_recognized"])

    def test_evaluation_rejects_modified_audio(self):
        with tempfile.TemporaryDirectory() as td:
            runtime, private = self._fixture(td)
            (runtime / "case-000000000000" / "clip_00_30.mp3").write_bytes(b"tampered")
            report = mod.evaluate(private.parent, SESSION)
            self.assertFalse(report["trials"][0]["audio_hash_matched"])
            self.assertFalse(report["trials"][0]["pass"])


if __name__ == "__main__":
    unittest.main()
