"""Synthetic-only regression: reuse MP3 blind audio to create MP4/AAC controls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import agy_mp4_audio_ab as ab


class MP4ABTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        self.old_truth = home / "AGYBlindAudio" / "truth"
        self.old_runtime = home / "AGYBlindAudio" / "runtime"
        self.new_truth = home / "AGYVideoAB" / "truth"
        self.new_runtime = home / "AGYVideoAB" / "runtime"
        self.source_session = "blind-0123456789abcdef"
        self.case = "case-0123456789ab"
        folder = self.old_runtime / self.source_session / self.case
        folder.mkdir(parents=True)
        self.original = folder / "clip_00_30.mp3"
        self.original.write_bytes(b"ID3synthetic 30-second MP3 fixture")
        manifest = {
            "schema": "agy_blind_audio_v2",
            "session": self.source_session,
            "runtime_root": str(self.old_runtime / self.source_session),
            "cases": [{"id": self.case, "sha256": hashlib.sha256(
                self.original.read_bytes()).hexdigest(),
                "kind": "speech",
                "reference": "파란 우산이 확인 번호는 일 이 삼 사 오 육 입니다.",
                "nonce": "일이삼사오육", "beep_count": 0}],
        }
        private = self.old_truth / self.source_session
        private.mkdir(parents=True)
        (private / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _prepare(self):
        def fake_process(argv, **kwargs):
            Path(argv[-1]).write_bytes(b"synthetic MP4")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with patch.object(ab, "_ffmpeg_binary", side_effect=lambda n: n), \
             patch.object(ab.subprocess, "run", side_effect=fake_process), \
             patch.object(ab, "_profile_matches", return_value=True), \
             patch.object(ab, "_probe_media", return_value={
                 "duration": 30.0, "streams": [
                     {"codec_type": "video", "codec_name": "h264"},
                     {"codec_type": "audio", "codec_name": "aac"}]}):
            return ab.prepare(self.source_session, self.old_truth,
                              self.new_runtime, self.new_truth)

    def test_video_marker_is_bitmap_and_contains_no_text_sidecar(self):
        target = Path(self.temp.name) / "frame.ppm"
        ab._frame(target, "012345")
        data = target.read_bytes()
        self.assertTrue(data.startswith(b"P6\n640 360\n255\n"))
        self.assertEqual(len(data), len(b"P6\n640 360\n255\n") + 640*360*3)
        self.assertNotIn(b"012345", data)

    def test_pcm_sound_profile_detects_similarity_and_silence(self):
        with patch.object(ab, "_audio_profile", side_effect=[
            ([100, 200, 0, 60], 100), ([95, 205, 0, 57], 100)]):
            self.assertTrue(ab._profile_matches(Path("a"), Path("b")))
        with patch.object(ab, "_audio_profile", side_effect=[
            ([0, 0, 0], 0), ([0, 0, 0], 0)]):
            self.assertTrue(ab._profile_matches(Path("a"), Path("b")))
        with patch.object(ab, "_audio_profile", side_effect=[
            ([100, 200, 0, 60], 100), ([0, 0, 0, 0], 0)]):
            self.assertFalse(ab._profile_matches(Path("a"), Path("b")))

    def test_preparation_does_not_touch_source_and_keeps_truth_outside_workspace(self):
        before = self.original.read_bytes()
        result = self._prepare()
        self.assertEqual(result["schema"], "agy_mp4_mp3_comparison_prepared_v1")
        self.assertEqual(result["case_count"], 1)
        self.assertFalse(result["model_called"])
        self.assertFalse(result["provider_audio_ingress_verified"])
        self.assertEqual(self.original.read_bytes(), before)
        workspace = Path(result["runtime_root"]) / self.case
        self.assertEqual([p.name for p in workspace.iterdir()], ["clip_00_30.mp4"])
        self.assertNotIn("일이삼사오육", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("일이삼사오육", (workspace / "clip_00_30.mp4")
                         .read_bytes().decode("utf-8"))
        private = Path(result["private_root"])
        record = json.loads((private / "manifest.json").read_text())
        self.assertEqual(record["cases"][0]["source_mp3_sha256"],
                         hashlib.sha256(before).hexdigest())
        self.assertEqual(len(record["cases"][0]["visual_code"]), 6)
        self.assertFalse(any(private.glob("*.ppm")))

    def test_import_and_score_are_offline_and_non_clobbering(self):
        prepared = self._prepare()
        session = prepared["session"]
        private = self.new_truth / session
        record = json.loads((private / "manifest.json").read_text())
        visual = record["cases"][0]["visual_code"]
        response = {"visual_code": visual, "acoustic_class": "SPEECH",
                    "beep_count": 0,
                    "segments": [{"text":
                        "파란 우산이 확인 번호는 일 이 삼 사 오 육 입니다."}]}
        raw = Path(self.temp.name) / "operator-response.json"
        raw.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        original_answer = self.old_runtime / self.source_session / self.case / "answer.json"
        original_answer.write_text(json.dumps({
            "verdict": "AUDIO_TRANSCRIBED", "audio_analyzed": True,
            "acoustic_class": "SPEECH", "beep_count": 0,
            "segments": [{"start": 0.0, "end": 3.0,
                          "text": response["segments"][0]["text"]}]},
            ensure_ascii=False), encoding="utf-8")
        with patch.object(ab.subprocess, "run",
                          side_effect=AssertionError("No subprocess/model call")):
            imported = ab.import_response(self.new_truth, session,
                                          self.case, raw)
            score = ab.score(self.new_truth, session)
        self.assertTrue(imported["recorded"])
        trial = score["trials"][0]
        self.assertTrue(trial["mp4_visual_code_match"])
        self.assertTrue(trial["mp4_audio_behavior_match"])
        self.assertTrue(trial["prior_mp3_audio_behavior_match"])
        self.assertEqual(trial["comparison"], "BOTH_BEHAVIORAL_MATCH")
        self.assertFalse(trial["provider_audio_ingress_verified"])
        self.assertFalse(score["private_evidence_modified"])
        self.assertNotIn(visual, json.dumps(score))
        self.assertNotIn("파란 우산이", json.dumps(score, ensure_ascii=False))
        with self.assertRaises(FileExistsError):
            ab.import_response(self.new_truth, session, self.case, raw)

    def test_audio_and_visual_outcomes_are_independent(self):
        prepared = self._prepare()
        session = prepared["session"]
        priv = self.new_truth / session
        (priv / "responses").mkdir()
        (priv / "responses" / (self.case + ".json")).write_text(
            json.dumps({"visual_code": "999999", "acoustic_class": "UNAVAILABLE",
                        "beep_count": None, "segments": []}), encoding="utf-8")
        scored = ab.score(self.new_truth, session)["trials"][0]
        self.assertFalse(scored["mp4_visual_code_match"])
        self.assertFalse(scored["mp4_audio_behavior_match"])
        self.assertIsNone(scored["prior_mp3_audio_behavior_match"])
        self.assertEqual(scored["comparison"], "INCOMPLETE")

    def test_no_response_keeps_unknown_instead_of_false(self):
        prepared = self._prepare()
        scored = ab.score(self.new_truth, prepared["session"])["trials"][0]
        self.assertIsNone(scored["mp4_visual_code_match"])
        self.assertIsNone(scored["mp4_audio_behavior_match"])
        self.assertEqual(scored["comparison"], "INCOMPLETE")

    def test_refuses_corrupt_source_hash_without_running_ffmpeg(self):
        self.original.write_bytes(b"TAMPERED")
        with patch.object(ab.subprocess, "run",
                          side_effect=AssertionError("No subprocess expected")):
            with self.assertRaises(ValueError):
                ab.prepare(self.source_session, self.old_truth,
                           self.new_runtime, self.new_truth)

    def test_model_output_cannot_be_imported_from_model_workspace(self):
        prepared = self._prepare()
        source = Path(prepared["runtime_root"]) / self.case / "result.json"
        source.write_text('{"visual_code":null,"acoustic_class":"UNAVAILABLE",'
                          '"beep_count":null,"segments":[]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            ab.import_response(self.new_truth, prepared["session"],
                               self.case, source)

    def test_strict_json_parse_rejects_prose_and_unbounded_fields(self):
        self.assertIsNone(ab._parse_answer("I heard speech, trust me"))
        self.assertIsNone(ab._parse_answer(json.dumps({
            "visual_code": None, "acoustic_class": "SPEECH",
            "segments": "NOT A LIST"})))
        self.assertEqual(ab._parse_answer(json.dumps({
            "visual_code": None, "acoustic_class": "UNAVAILABLE",
            "segments": [], "beep_count": None}))["acoustic_class"],
            "UNAVAILABLE")

    def test_no_submit_option_or_model_launch_in_public_cli(self):
        self.assertNotIn("--submit", ab.PROMPT)
        self.assertNotIn("일이삼사오육", ab.PROMPT)
        self.assertFalse(any(k in ab.PROMPT.lower()
                             for k in ("access token", "api key")))


if __name__ == "__main__":
    unittest.main()
