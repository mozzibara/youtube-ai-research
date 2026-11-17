"""Offline byte-level audit of archived AGY read_file tool results.

This is a separate host-side verifier, not a model invocation or a packet
capture. A SHA-256 match inside AGY's own tool-result stream is evidence of
CLI-visible bytes, NOT independent proof of Gemini/model ingress.
No raw media, private paths, transcript text or decoded payload are emitted.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess  # Reserved for test-enforced guarantee: no process is run.

SESSION_RE = re.compile(r"blind-[0-9a-f]{16}\Z")
CASE_RE = re.compile(r"case-[0-9a-f]{12}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_TRACE_BYTES = 25 * 1024 * 1024
MAX_MEDIA_BYTES = 12 * 1024 * 1024
MAX_BASE64_CHARS = ((MAX_MEDIA_BYTES + 2) // 3) * 4 + 40
MAX_DEPTH = 10
AUDIO_MIMES = frozenset({"audio/mpeg", "audio/mp3", "audio/x-mp3"})


def _regular_file(path: Path, maximum: int) -> bool:
    """Refuse links/oversized inputs; never follow unexpected artifact symlinks."""
    try:
        return (not path.is_symlink() and path.is_file()
                and 0 < path.stat().st_size <= maximum)
    except OSError:
        return False


def _read_manifest(private_root: Path, session: str) -> tuple[Path, dict]:
    if not SESSION_RE.fullmatch(session):
        raise ValueError("Invalid session identifier")
    priv = private_root.resolve() / session
    manifest_path = priv / "manifest.json"
    if priv.is_symlink() or not _regular_file(manifest_path, MAX_MANIFEST_BYTES):
        raise ValueError("Missing or unsafe private manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid private manifest") from exc
    if (not isinstance(manifest, dict)
            or manifest.get("schema") not in {"agy_blind_audio_v1", "agy_blind_audio_v2"}
            or manifest.get("session") != session
            or not isinstance(manifest.get("runtime_root"), str)
            or not isinstance(manifest.get("cases"), list)
            or not 1 <= len(manifest["cases"]) <= 30):
        raise ValueError("Invalid private manifest contract")
    runtime = Path(manifest["runtime_root"])
    if (not runtime.is_absolute() or runtime.is_symlink()
            or runtime.name != session):
        raise ValueError("Unsafe runtime root in private manifest")
    seen = set()
    for item in manifest["cases"]:
        if (not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not CASE_RE.fullmatch(item["id"])
                or item["id"] in seen
                or not isinstance(item.get("sha256"), str)
                or not SHA_RE.fullmatch(item["sha256"])):
            raise ValueError("Invalid private case identifier or hash")
        seen.add(item["id"])
    return priv, manifest


def _target_argument(params: object) -> bool:
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            return False
    if not isinstance(params, dict):
        return False
    for key in ("file_path", "path"):
        value = params.get(key)
        if isinstance(value, str) and value.replace("\\", "/").rsplit("/", 1)[-1].casefold() == "clip_00_30.mp3":
            return True
    return False


def _call_id(info: dict, step: dict) -> str | None:
    for mapping in (info, step):
        for key in ("tool_call_id", "call_id", "id"):
            value = mapping.get(key)
            if isinstance(value, str) and 1 <= len(value) <= 128:
                return value
    return None


def _payload_candidates(value: object, depth: int = 0):
    """Accept only typed audio bytes within a tool *result* subtree."""
    if depth >= MAX_DEPTH:
        return
    if isinstance(value, dict):
        mime = next((value[k] for k in ("mime_type", "mimeType", "media_type")
                     if isinstance(value.get(k), str)), None)
        if isinstance(mime, str) and mime.lower().split(";", 1)[0].strip() in AUDIO_MIMES:
            for field in ("data", "base64", "base64_data"):
                if isinstance(value.get(field), str):
                    yield value[field]
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _payload_candidates(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:100]:
            if isinstance(child, (dict, list)):
                yield from _payload_candidates(child, depth + 1)


def _bytes_match(encoded: str, size: int, digest: str) -> str:
    if encoded.lower().startswith("data:"):
        comma = encoded.find(",")
        if comma < 0:
            return "INVALID"
        prefix = encoded[:comma].lower()
        if prefix not in {"data:audio/mpeg;base64", "data:audio/mp3;base64",
                          "data:audio/x-mp3;base64"}:
            return "INVALID"
        encoded = encoded[comma + 1:]
    if not encoded or len(encoded) > MAX_BASE64_CHARS:
        return "INVALID"
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return "INVALID"
    if len(raw) > MAX_MEDIA_BYTES:
        return "INVALID"
    return "MATCH" if len(raw) == size and hashlib.sha256(raw).hexdigest() == digest else "MISMATCH"


def _scan_trace(trace: Path, size: int, digest: str) -> dict:
    """Correlate by typed tool event, not by adjacency or model-authored text."""
    rows = []
    invalid = 0
    with trace.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                invalid += 1
                continue
            if isinstance(item, dict):
                rows.append(item)
    ids = set()
    results = []
    target_calls = 0
    for row in rows:
        if row.get("event") != "step_update":
            continue
        step = row.get("step_update")
        if not isinstance(step, dict) or step.get("step_type") != "tool":
            continue
        info = step.get("tool_info")
        if not isinstance(info, dict) or info.get("name", "").split(".")[-1] != "read_file":
            continue
        if not _target_argument(info.get("parameters")):
            continue
        target_calls += 1
        identifier = _call_id(info, step)
        if identifier is not None:
            ids.add(identifier)
        # Never inspect parameters: they are model-supplied arguments.
        if step.get("status") not in {"FAILED", "ERROR"}:
            for key in ("result", "output", "response"):
                if key in info:
                    results.append(info[key])
    for row in rows:
        if row.get("event") == "tool_result" and row.get("source") == "TOOL":
            identifier = row.get("tool_call_id") or row.get("call_id")
            if isinstance(identifier, str) and identifier in ids:
                results.append(row.get("result"))
        elif row.get("event") == "step_update":
            step = row.get("step_update")
            if (isinstance(step, dict) and step.get("step_type") == "tool_result"
                    and step.get("source") == "TOOL"
                    and step.get("tool_call_id") in ids):
                results.append(step.get("result"))

    candidate_count = 0
    exact = 0
    mismatched = 0
    invalid_encoding = 0
    for item in results:
        for encoded in _payload_candidates(item):
            candidate_count += 1
            label = _bytes_match(encoded, size, digest)
            if label == "MATCH":
                exact += 1
            elif label == "MISMATCH":
                mismatched += 1
            else:
                invalid_encoding += 1
    if invalid:
        verdict = "INCOMPLETE_TRACE"
    elif not target_calls:
        verdict = "NO_TARGET_READ_CALL"
    elif not candidate_count:
        verdict = "NO_TOOL_RESULT_AUDIO_BYTES"
    elif exact and not mismatched and not invalid_encoding:
        verdict = "MATCHED_CLI_TOOL_RESULT_BYTES"
    elif exact:
        verdict = "AMBIGUOUS_TOOL_RESULT_BYTES"
    elif mismatched:
        verdict = "CLI_TOOL_RESULT_BYTES_MISMATCH"
    else:
        verdict = "INVALID_TOOL_RESULT_ENCODING"
    return {
        "assessment": verdict,
        "target_read_call_observed": bool(target_calls),
        "tool_result_audio_payload_count": candidate_count,
        "inline_audio_payload_seen": bool(candidate_count),
        "exact_mp3_bytes_in_tool_result": bool(exact) and verdict == "MATCHED_CLI_TOOL_RESULT_BYTES",
        "malformed_trace_lines_observed": bool(invalid),
    }


def _case_report(priv: Path, manifest: dict, item: dict) -> dict:
    case_id = item["id"]
    result = {
        "trial": case_id,
        "assessment": "UNKNOWN",
        "target_read_call_observed": False,
        "tool_result_audio_payload_count": 0,
        "inline_audio_payload_seen": False,
        "exact_mp3_bytes_in_tool_result": False,
        "malformed_trace_lines_observed": False,
        # A local CLI trace is never an independently trusted provider receipt.
        "model_ingress_verified": False,
        "independent_binary_transport_verified": False,
        "evidence_boundary": "AGY_CLI_STREAM_NOT_PROVIDER_INGRESS",
    }
    workspace = Path(manifest["runtime_root"]) / case_id
    media = workspace / "clip_00_30.mp3"
    if workspace.is_symlink() or not _regular_file(media, MAX_MEDIA_BYTES):
        result["assessment"] = "MEDIA_UNAVAILABLE_OR_UNSAFE"
        return result
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    if digest != item["sha256"]:
        result["assessment"] = "MEDIA_HASH_MISMATCH"
        return result
    trace = priv / (case_id + ".ndjson")
    if trace.is_symlink() or (trace.exists() and not _regular_file(trace, MAX_TRACE_BYTES)):
        result["assessment"] = "UNSAFE_TRACE"
    elif not trace.exists():
        result["assessment"] = "TRACE_MISSING"
    else:
        result.update(_scan_trace(trace, media.stat().st_size, digest))
    return result


def audit_session(private_root: Path, session: str) -> dict:
    """Read-only, fully redacted report. Never contacts AGY or rewrites archives."""
    priv, manifest = _read_manifest(private_root, session)
    reports = [_case_report(priv, manifest, item) for item in manifest["cases"]]
    return {
        "schema": "agy_cli_audio_byte_audit_v1",
        "session": session,
        "model_called": False,
        "private_evidence_modified": False,
        "independent_binary_transport_verified": False,
        "trials": reports,
        "note": ("A byte match is CLI tool-result visibility only; proving model "
                 "ingress requires independently authenticated provider-side evidence."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline AGY MP3 tool-result byte audit (no model requests)")
    parser.add_argument("--session", required=True)
    parser.add_argument("--private-root", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.private_root is None:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            parser.error("Provide --private-root outside Windows")
        private_root = Path(local) / "AGYBlindAudio" / "truth"
    else:
        private_root = args.private_root
    try:
        report = audit_session(private_root, args.session)
    except (ValueError, OSError):
        print(json.dumps({"schema": "agy_cli_audio_byte_audit_v1",
                          "status": "AUDIT_UNAVAILABLE_OR_INVALID_EVIDENCE",
                          "model_called": False}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
