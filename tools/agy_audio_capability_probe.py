"""Read-only AGY CLI version/help capability probe; NEVER submits a model prompt.

The only child processes are the located AGY executable with exactly
--version or --help. Results are redacted summaries, never raw process
output, paths, credentials, or private media. This is a capability hint,
not a native attachment or provider-ingress verification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

DOCUMENTED_AUDIO_RELEASE = (1, 1, 18)
AUDIO_DOC_URL = "https://antigravity.google/docs/changelog?tab=cli"
PROMPT_DOC_URL = "https://antigravity.google/docs/cli/prompting"
MAX_OUTPUT_CHARS = 200_000
COMMAND_TIMEOUT_SECONDS = 8
# No arbitrary CLI flags, commands, positional prompts or user-supplied args.
SAFE_ARGS = ("--version", "--help")
ATTACHMENT_FLAG_ALLOWLIST = frozenset({
    "--audio", "--audio-file", "--input-audio", "--attach", "--attachment",
    "--attachments", "--attach-file", "--attach-media", "--media-file",
    "--input-media",
})
FLAG_LINE = re.compile(r"(?m)^[ \t]*(?:-[A-Za-z],?[ \t]+)?(--[a-z][a-z0-9-]*)\b")
VERSION_RE = re.compile(r"(?<![\d.])v?(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])", re.I)
AUDIO_WORD = re.compile(r"\b(?:audio|mp3|wav|m4a|flac|opus|attachments?)\b", re.I)


def find_audio_attachment_flags(help_text: str) -> list[str]:
    """Only explicit option declarations, never words in descriptions."""
    if not isinstance(help_text, str):
        return []
    return sorted({
        match.group(1) for match in FLAG_LINE.finditer(help_text[:MAX_OUTPUT_CHARS])
        if match.group(1) in ATTACHMENT_FLAG_ALLOWLIST
    })


def _parse_version(raw: str) -> tuple[str | None, tuple[int, int, int] | None]:
    if not isinstance(raw, str):
        return None, None
    match = VERSION_RE.search(raw[:8_192])
    if not match:
        return None, None
    parts = tuple(int(s) for s in match.groups())
    return ".".join(str(x) for x in parts), parts


def _run_safe(cli: str, flag: str, cwd: str) -> tuple[str, str]:
    if flag not in SAFE_ARGS:
        raise ValueError("Only version and help inspection are permitted")
    env = os.environ.copy()
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    env["NO_COLOR"] = "1"
    env["CI"] = "1"
    try:
        completed = subprocess.run(
            [cli, flag], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace",
            check=False, shell=False, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "TIMEOUT", ""
    except OSError:
        return "PROCESS_UNAVAILABLE", ""
    if completed.returncode != 0:
        return "NONZERO_EXIT", ""
    # Output is held in memory only and never returned or stored verbatim.
    return "OK", (completed.stdout or "")[:MAX_OUTPUT_CHARS]


def _base_report() -> dict:
    return {
        "schema": "agy_installed_audio_capability_v1",
        "model_called": False,
        "prompt_submitted": False,
        "audio_file_opened": False,
        "private_evidence_modified": False,
        "probe_scope": "INSTALLED_CLI_VERSION_AND_HELP_ONLY",
        "probed_arguments": [],
        "version_status": "NOT_RUN",
        "help_status": "NOT_RUN",
        "installed_cli_version": None,
        "release_note_version_assessment": "VERSION_UNKNOWN",
        "audio_attachment_flags": [],
        "help_audio_keyword_seen": False,
        "assessment": "INDETERMINATE",
        "native_audio_attachment_verified": False,
        "provider_audio_ingress_verified": False,
        "independent_binary_transport_verified": False,
        "sources": {
            "audio_format_release_notes": AUDIO_DOC_URL,
            "prompt_clipboard_media_docs": PROMPT_DOC_URL,
        },
        "limitations": (
            "Only --version and --help are invoked (no prompt or model request). "
            "Release-note version and help flags do not establish a working audio "
            "attachment path, a payload hash, or provider model ingress. CLI "
            "execution may still perform its own startup housekeeping/telemetry."
        ),
    }


def probe_installed_cli() -> dict:
    """Interrogate the first PATH-resolved AGY launcher in a temporary cwd.

    Does not read source MP3, AGY settings, authentication, log files, or
    shell profiles. Fails closed on timeouts, errors and unknown versions.
    """
    report = _base_report()
    cli = shutil.which("agy")
    if not cli:
        report["version_status"] = "CLI_NOT_FOUND"
        report["help_status"] = "CLI_NOT_FOUND"
        report["assessment"] = "INSTALLED_CLI_UNAVAILABLE"
        return report
    # Prevent a PATH accident from causing a generic script to run.
    if Path(cli).suffix.lower() not in {"", ".exe", ".cmd", ".bat"}:
        report["version_status"] = "UNSUPPORTED_LAUNCHER"
        report["help_status"] = "UNSUPPORTED_LAUNCHER"
        report["assessment"] = "UNSUPPORTED_CLI_LAUNCHER"
        return report
    with tempfile.TemporaryDirectory(prefix="agy-audio-capability-") as cwd:
        report["probed_arguments"].append("--version")
        status, output = _run_safe(cli, "--version", cwd)
        version, parsed = _parse_version(output) if status == "OK" else (None, None)
        report["version_status"] = (
            "UNRECOGNIZED_VERSION" if status == "OK" and parsed is None
            else status)
        report["installed_cli_version"] = version
        if parsed is not None:
            report["release_note_version_assessment"] = (
                "AT_OR_ABOVE_DOCUMENTED_AUDIO_RELEASE"
                if parsed >= DOCUMENTED_AUDIO_RELEASE
                else "BELOW_DOCUMENTED_AUDIO_RELEASE")
        report["probed_arguments"].append("--help")
        hstatus, help_text = _run_safe(cli, "--help", cwd)
        report["help_status"] = hstatus
        if hstatus == "OK":
            report["audio_attachment_flags"] = find_audio_attachment_flags(help_text)
            report["help_audio_keyword_seen"] = bool(AUDIO_WORD.search(help_text))
    if report["help_status"] != "OK":
        report["assessment"] = "HELP_NOT_AVAILABLE_ATTACHMENT_UNKNOWN"
    elif report["audio_attachment_flags"]:
        report["assessment"] = "AUDIO_NAMED_FLAG_CANDIDATE_ATTACHMENT_NOT_VERIFIED"
    elif parsed is None:
        report["assessment"] = "VERSION_UNKNOWN_ATTACHMENT_UNVERIFIED"
    elif parsed < DOCUMENTED_AUDIO_RELEASE:
        report["assessment"] = "OLDER_BUILD_AUDIO_SUPPORT_UNKNOWN"
    else:
        report["assessment"] = "DOCUMENTED_AUDIO_FORMAT_EXPECTED_ENTRYPOINT_UNVERIFIED"
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect the installed agy --version / --help only; NO model calls")
    parser.parse_args(argv)
    try:
        report = probe_installed_cli()
    except (ValueError, OSError):
        report = _base_report()
        report["assessment"] = "PROBE_UNAVAILABLE"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
