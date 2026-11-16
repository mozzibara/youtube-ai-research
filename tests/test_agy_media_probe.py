"""Offline contract tests for the public source-only AGY media probe."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "tools" / "agy_media_probe.py"
spec = importlib.util.spec_from_file_location("agy_media_probe", PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class AgymediaContractTests(unittest.TestCase):
    def test_audio_prompt_is_single_line_and_has_no_reference_text(self):
        text = probe.safe_prompt("audio")
        self.assertNotIn("\n", text)
        self.assertIn("NO_AUDIO_ACCESS", text)
        self.assertIn("flash_audio.json", text)
        self.assertIn("native_multimodal_audio_attachment", text)
        self.assertNotIn("https://", text)

    def test_mp3_prompt_and_result_are_separate_from_wav(self):
        self.assertEqual(probe.audio_result_filename("audio", "wav"), "flash_audio.json")
        self.assertEqual(probe.audio_result_filename("audio", "mp3"), "flash_audio_mp3.json")
        self.assertEqual(probe.audio_result_filename("video", "wav"), "flash_mp4.json")
        self.assertIn("flash_audio_mp3.json", probe.safe_prompt("audio", "mp3"))

    def test_mp3_probe_is_dry_run_only_with_no_media_generation(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            workspace = folder / "agy_clean_workspace"
            workspace.mkdir()
            (workspace / "clip_00_30.mp4").write_bytes(b"synthetic fixture")
            with patch.object(probe, "prepare_mp3", side_effect=AssertionError("not dry")):
                self.assertEqual(probe.main(["--run", str(folder),
                                             "--media-type", "audio",
                                             "--audio-format", "mp3",
                                             "--dry-run"]), 0)

    def test_mp3_audio_is_kept_separate_from_previous_failure(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "flash_audio.json").write_text('{"verdict":"NO_AUDIO_ACCESS"}')
            self.assertNotEqual(folder / probe.audio_result_filename("audio", "mp3"),
                                folder / "flash_audio.json")

    def test_video_prompt_is_distinct(self):
        text = probe.safe_prompt("video")
        self.assertIn("flash_mp4.json", text)
        self.assertIn("native_multimodal_video_attachment", text)

    def test_required_flags_kept_in_launcher(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            script, marker = probe.launcher(folder, "AGY_MEDIA_0123456789", r"C:\tools\agy.cmd")
            text = script.read_text(encoding="utf-8-sig")
            self.assertIn("--dangerously-skip-permissions", text)
            self.assertIn("--sandbox", text)
            self.assertIn("gemini-3.8-flash-medium", text)
            self.assertFalse(marker.exists())

    def test_safe_launcher_marker(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                probe.launcher(Path(td), "bad'; rm -rf", "agy")

    def test_only_new_window_can_be_selected(self):
        before = {10}
        after = [(10, "AGY_MEDIA_0123456789", object()),
                 (11, "other", object())]
        self.assertIsNone(probe.choose_window(before, after, "AGY_MEDIA_0123456789"))
        after[1] = (11, "AGY_MEDIA_0123456789", object())
        self.assertEqual(probe.choose_window(before, after, "AGY_MEDIA_0123456789"), 11)

    def test_ambiguous_window_refused(self):
        after = [(10, "AGY_MEDIA_0123456789", object()),
                 (11, "AGY_MEDIA_0123456789", object())]
        self.assertIsNone(probe.choose_window(set(), after, "AGY_MEDIA_0123456789"))

    def _file(self, root, verdict="AUDIO_TRANSCRIBED", audio=True):
        file = Path(root) / "result.json"
        file.write_text(json.dumps({
            "input": {"attached_audio_seen": True,
                      "audio_analyzed": audio,
                      "input_method": "native_multimodal_audio_attachment"},
            "segments": [{"start": 1.0, "end": 2.0, "text": "안녕하세요"}],
            "verdict": verdict,
        }), encoding="utf-8")
        return file

    def test_valid_audio_result(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(len(probe.validate_result(self._file(td), "audio")["segments"]), 1)

    def test_no_audio_access_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                probe.validate_result(self._file(td, verdict="NO_AUDIO_ACCESS"), "audio")

    def test_no_audio_flag_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                probe.validate_result(self._file(td, audio=False), "audio")

    def test_wrong_media_type_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                probe.validate_result(self._file(td), "video")

    def test_dry_run_does_not_invoke_model_or_modify_clipboard(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            workspace = folder / "agy_clean_workspace"
            workspace.mkdir()
            (workspace / "clip_00_30.mp4").write_bytes(b"synthetic fixture")
            with patch.object(probe, "run_gui", side_effect=AssertionError("GUI called")):
                self.assertEqual(probe.main(["--run", str(folder), "--dry-run"]), 0)

    def test_synthetic_caption_compare_not_recognition_accuracy(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            references = folder / "references"
            references.mkdir()
            for name, text in [("auto_ko_00_30.json", "안녕하세요"),
                               ("manual_ko_00_30.json", "안녕하세요 여러분")]:
                (references / name).write_text(
                    json.dumps({"events": [{"start": 0, "end": 2, "text": text}]}),
                    encoding="utf-8")
            obj = {"segments": [{"start": 0, "end": 2, "text": "안녕하세요 여러분"}]}
            result = probe.compare_references(folder, obj, "audio")
            self.assertIsNotNone(result)
            self.assertFalse(result["human_audio_ground_truth_verified"])
            self.assertTrue((folder / "comparison_audio.json").is_file())


if __name__ == "__main__":
    unittest.main()
