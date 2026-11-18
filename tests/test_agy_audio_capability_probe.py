"""No-model AGY CLI installed-version capability probe tests (synthetic only)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import agy_audio_capability_probe as probe


def outcome(text: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=text, stderr="")


class AGYAudioCapabilityTests(unittest.TestCase):
    def test_version_at_documented_baseline_without_help_flag_is_unverified(self):
        with patch.object(probe.shutil, "which", return_value="C:/Tools/agy.exe"), \
             patch.object(probe.subprocess, "run", side_effect=[
                 outcome("Antigravity CLI v1.5.9"),
                 outcome("Usage: agy [options]\n  -p --prompt  print\n  --input-format text"),
             ]) as run:
            report = probe.probe_installed_cli()
        self.assertEqual(report["schema"], "agy_installed_audio_capability_v1")
        self.assertEqual(report["installed_cli_version"], "1.5.9")
        self.assertEqual(report["version_status"], "OK")
        self.assertEqual(report["help_status"], "OK")
        self.assertEqual(report["release_note_version_assessment"],
                         "AT_OR_ABOVE_DOCUMENTED_AUDIO_RELEASE")
        self.assertEqual(report["audio_attachment_flags"], [])
        self.assertEqual(report["assessment"],
                         "DOCUMENTED_AUDIO_FORMAT_EXPECTED_ENTRYPOINT_UNVERIFIED")
        self.assertEqual(report["probed_arguments"], ["--version", "--help"])
        self.assertFalse(report["model_called"])
        self.assertFalse(report["prompt_submitted"])
        self.assertFalse(report["native_audio_attachment_verified"])
        self.assertFalse(report["provider_audio_ingress_verified"])
        self.assertNotIn("C:/Tools", json.dumps(report))
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[0], "C:/Tools/agy.exe")
            self.assertIn(argv[1], {"--version", "--help"})
            self.assertEqual(len(argv), 2)
            self.assertFalse(call.kwargs.get("shell", False))
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"],
                             "true")

    def test_explicit_attachment_flag_is_candidate_not_verified(self):
        with patch.object(probe.shutil, "which", return_value="/tmp/agy"), \
             patch.object(probe.subprocess, "run", side_effect=[
                 outcome("agy 1.5.9"),
                 outcome("Usage: agy [flags]\n  --audio-file <path> Attach MP3 audio"),
             ]):
            report = probe.probe_installed_cli()
        self.assertEqual(report["audio_attachment_flags"], ["--audio-file"])
        self.assertEqual(report["assessment"],
                         "AUDIO_NAMED_FLAG_CANDIDATE_ATTACHMENT_NOT_VERIFIED")
        self.assertTrue(report["help_audio_keyword_seen"])
        self.assertFalse(report["native_audio_attachment_verified"])

    def test_audio_keyword_inside_help_is_not_an_attachment_flag(self):
        with patch.object(probe.shutil, "which", return_value="/tmp/agy"), \
             patch.object(probe.subprocess, "run", side_effect=[
                 outcome("1.1.18"),
                 outcome("Usage: agy --help\n"
                         "  --model MODEL   Audio capable model\n"
                         "  --input-format FORMAT stream-json, text\n"
                         "  --voice   live dictation"),
             ]):
            report = probe.probe_installed_cli()
        self.assertEqual(report["audio_attachment_flags"], [])
        self.assertTrue(report["help_audio_keyword_seen"])
        self.assertEqual(report["assessment"],
                         "DOCUMENTED_AUDIO_FORMAT_EXPECTED_ENTRYPOINT_UNVERIFIED")

    def test_version_before_release_note_is_not_claimed_as_unsupported(self):
        with patch.object(probe.shutil, "which", return_value="/tmp/agy"), \
             patch.object(probe.subprocess, "run", side_effect=[
                 outcome("Antigravity CLI 1.1.17"), outcome("Usage: agy"),
             ]):
            report = probe.probe_installed_cli()
        self.assertEqual(report["release_note_version_assessment"],
                         "BELOW_DOCUMENTED_AUDIO_RELEASE")
        self.assertEqual(report["assessment"], "OLDER_BUILD_AUDIO_SUPPORT_UNKNOWN")
        self.assertFalse(report["native_audio_attachment_verified"])

    def test_missing_cli_does_not_call_any_command(self):
        with patch.object(probe.shutil, "which", return_value=None), \
             patch.object(probe.subprocess, "run", side_effect=AssertionError(
                 "Must not invoke any binary")):
            report = probe.probe_installed_cli()
        self.assertEqual(report["version_status"], "CLI_NOT_FOUND")
        self.assertEqual(report["assessment"], "INSTALLED_CLI_UNAVAILABLE")
        self.assertEqual(report["probed_arguments"], [])
        self.assertNotIn("C:", json.dumps(report))

    def test_help_failure_and_timeout_are_fail_closed(self):
        with patch.object(probe.shutil, "which", return_value="/tmp/agy"), \
             patch.object(probe.subprocess, "run", side_effect=[
                 outcome("agy v1.5.9"),
                 subprocess.TimeoutExpired(cmd=["agy", "--help"], timeout=6),
             ]):
            report = probe.probe_installed_cli()
        self.assertEqual(report["help_status"], "TIMEOUT")
        self.assertEqual(report["assessment"], "HELP_NOT_AVAILABLE_ATTACHMENT_UNKNOWN")
        self.assertFalse(report["native_audio_attachment_verified"])

    def test_invalid_version_does_not_leak_raw_error_or_guess(self):
        private = "PRIVATE_API_TOKEN=SUPERSECRET"
        with patch.object(probe.shutil, "which", return_value="/tmp/agy"), \
             patch.object(probe.subprocess, "run", side_effect=[
                 outcome(private), outcome("Usage: agy\n --help"),
             ]):
            report = probe.probe_installed_cli()
        self.assertEqual(report["version_status"], "UNRECOGNIZED_VERSION")
        self.assertIsNone(report["installed_cli_version"])
        self.assertEqual(report["release_note_version_assessment"],
                         "VERSION_UNKNOWN")
        self.assertNotIn(private, json.dumps(report))

    def test_nonzero_exit_does_not_extract_false_version(self):
        with patch.object(probe.shutil, "which", return_value="/tmp/agy"), \
             patch.object(probe.subprocess, "run", side_effect=[
                 outcome("agy 1.5.9", code=1), outcome("Usage: agy"),
             ]):
            report = probe.probe_installed_cli()
        self.assertEqual(report["version_status"], "NONZERO_EXIT")
        self.assertIsNone(report["installed_cli_version"])

    def test_local_files_and_credentials_are_never_read(self):
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root) / "token.txt"
            secret.write_text("PRIVATE_PASSWORD", encoding="utf-8")
            before = secret.read_bytes()
            with patch.object(probe.shutil, "which", return_value="/tmp/agy"), \
                 patch.object(probe.subprocess, "run", side_effect=[
                     outcome("Antigravity v1.5.9 user=/private/home"),
                     outcome("Usage: agy\n--audio-file <SECRET_TOKEN>"),
                 ]):
                report = probe.probe_installed_cli()
            self.assertEqual(secret.read_bytes(), before)
            self.assertNotIn("PRIVATE_PASSWORD", json.dumps(report))
            self.assertNotIn("<SECRET_TOKEN>", json.dumps(report))

    def test_help_flag_parser_rejects_words_in_body(self):
        flags = probe.find_audio_attachment_flags(
            "Usage: agy --audio-file\n"
            "The string '--attach' is an example, not an option\n"
            "  --input-format text\n"
            "  --attachment AUDIOFILE   file attachment")
        self.assertEqual(flags, ["--attachment"])

    def test_unsupported_platform_launcher_fails_closed(self):
        with patch.object(probe.shutil, "which", return_value="agy.ps1"), \
             patch.object(probe.subprocess, "run", side_effect=AssertionError(
                 "No execution of scripts")):
            report = probe.probe_installed_cli()
        self.assertEqual(report["assessment"], "UNSUPPORTED_CLI_LAUNCHER")


if __name__ == "__main__":
    unittest.main()
