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

# Both entrypoints are supported: "python tools/..." and "python -m tools...".
if __package__:
    from .agy_blind_audio import inspect_headless_trace
else:
    from agy_blind_audio import inspect_headless_trace

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
        if not isinstance(info, dict):
            continue
        # This mirrors the already-deployed blind evaluator's event vocabulary.
        # AGY may store the tool name at step.tool_name rather than tool_info.name.
        raw_name = info.get("name")
        if not isinstance(raw_name, str):
            raw_name = step.get("tool_name")
        if not isinstance(raw_name, str) or raw_name.split(".")[-1] != "read_file":
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
                    and isinstance(step.get("tool_call_id"), str)
                    and step["tool_call_id"] in ids):
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
        "legacy_provenance_only": False,
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
        # Reconcile against the established provenance parser used by
        # evaluate/recover, which already recognized the user's archived
        # target read_file calls. A protocol mismatch is not "no read call".
        # Crucially, this fallback NEVER consumes an audio blob or asserts
        # a byte match: strict payload correlation is still mandatory.
        if result["assessment"] == "NO_TARGET_READ_CALL":
            prior = inspect_headless_trace(trace)
            if prior["mp3_read_call_observed"]:
                result["assessment"] = "TARGET_READ_CALL_SHAPE_UNSUPPORTED"
                result["target_read_call_observed"] = True
                result["legacy_provenance_only"] = True
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



# Safe labels only. Never emit arbitrary input-parameter names: they may be paths,
# tokens, prompt content, or sensitive names masquerading as JSON dictionary keys.
VISIBLE_PARAMETER_KEYS = frozenset({
    "file_path", "path", "filename", "file", "uri", "absolute_path",
    "relative_path", "target_path", "filepath", "filePath",
})
VISIBLE_TOOL_RESULT_KEYS = ("result", "output", "response")


def _parameter_envelope(params: object) -> tuple[str, object]:
    """Classify serialization without printing values or dynamic keys."""
    if isinstance(params, dict):
        return "OBJECT", params
    if isinstance(params, list):
        return "ARRAY", params
    if isinstance(params, str):
        try:
            parsed = json.loads(params)
        except (ValueError, TypeError):
            return "TEXT", params
        if isinstance(parsed, dict):
            return "JSON_OBJECT_TEXT", parsed
        if isinstance(parsed, list):
            return "JSON_ARRAY_TEXT", parsed
        return "JSON_SCALAR_TEXT", parsed
    if params is None:
        return "NULL", params
    return "SCALAR", params


def _legacy_target_reference(params: object) -> bool:
    """Match only the already-used blind-evaluator's unscoped hint.

    This is an observation of serialized arguments, not a verified file path.
    It must never authorize binary byte matching.
    """
    try:
        return "clip_00_30.mp3" in json.dumps(
            params, ensure_ascii=False).casefold()
    except (TypeError, ValueError):
        return False


def _probe_trace_shape(path: Path) -> dict:
    """Inspect known AGY stream metadata only, leaving raw records untouched."""
    read_events = 0
    legacy_matches = 0
    strict_matches = 0
    root_types: set[str] = set()
    known_keys: set[str] = set()
    result_keys: set[str] = set()
    name_origins: set[str] = set()
    unknown_keys = 0
    invalid_lines = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                invalid_lines += 1
                continue
            if not isinstance(row, dict) or row.get("event") != "step_update":
                continue
            step = row.get("step_update")
            if not isinstance(step, dict) or step.get("step_type") != "tool":
                continue
            info = step.get("tool_info")
            if not isinstance(info, dict):
                continue
            name = info.get("name") or step.get("tool_name")
            if not isinstance(name, str) or name.split(".")[-1] != "read_file":
                continue
            read_events += 1
            if isinstance(info.get("name"), str) and info.get("name"):
                name_origins.add("TOOL_INFO_NAME")
            else:
                name_origins.add("STEP_TOOL_NAME")
            params = info.get("parameters")
            kind, parsed = _parameter_envelope(params)
            root_types.add(kind)
            if isinstance(parsed, dict):
                known_keys.update(k for k in parsed if k in VISIBLE_PARAMETER_KEYS)
                unknown_keys += sum(k not in VISIBLE_PARAMETER_KEYS for k in parsed)
            strict_matches += int(_target_argument(params))
            legacy_matches += int(_legacy_target_reference(params))
            result_keys.update(k for k in VISIBLE_TOOL_RESULT_KEYS if k in info)
    scope = (
        "STRICT_KNOWN_PATH_FIELD" if strict_matches
        else "UNSCOPED_LEGACY_REFERENCE" if legacy_matches
        else "NO_LEGACY_TARGET_EVENT"
    )
    return {
        "trace_status": "INCOMPLETE_TRACE" if invalid_lines else "TRACE_PRESENT",
        "read_file_tool_events": read_events,
        "legacy_target_read_calls": legacy_matches,
        "strict_target_read_calls": strict_matches,
        "target_argument_shape": scope,
        "parameter_envelope_types": sorted(root_types),
        "parameter_known_keys": sorted(known_keys),
        "unrecognized_top_level_keys": unknown_keys,
        "tool_name_origins": sorted(name_origins),
        "observed_tool_result_fields": sorted(result_keys),
    }


def probe_session(private_root: Path, session: str) -> dict:
    """Redacted, read-only protocol discovery for previously archived trials.

    Does not read MP3/audio bytes or score speech; does not call any model.
    All output strings are fixed internal enums or allowlisted field labels.
    """
    priv, manifest = _read_manifest(private_root, session)
    trials = []
    for item in manifest["cases"]:
        case_id = item["id"]
        trace = priv / (case_id + ".ndjson")
        report = {
            "trial": case_id,
            "trace_status": "TRACE_MISSING",
            "read_file_tool_events": 0,
            "legacy_target_read_calls": 0,
            "strict_target_read_calls": 0,
            "target_argument_shape": "NO_LEGACY_TARGET_EVENT",
            "parameter_envelope_types": [],
            "parameter_known_keys": [],
            "unrecognized_top_level_keys": 0,
            "tool_name_origins": [],
            "observed_tool_result_fields": [],
            "independent_binary_transport_verified": False,
        }
        if trace.is_symlink() or (trace.exists() and
                                 not _regular_file(trace, MAX_TRACE_BYTES)):
            report["trace_status"] = "UNSAFE_TRACE"
        elif trace.exists():
            report.update(_probe_trace_shape(trace))
        trials.append(report)
    return {
        "schema": "agy_cli_audio_event_shape_v1",
        "session": session,
        "model_called": False,
        "private_evidence_modified": False,
        "independent_binary_transport_verified": False,
        "trials": trials,
        "note": ("Inspects existing tool-event structure only; names/values of "
                 "unrecognized fields and any actual media content are hidden. "
                 "No proof of provider-side audio ingress."),
    }



# These fixed output categories never depend on arbitrary input key names.
LIFECYCLE_TARGET = "clip_00_30.mp3"
LIFECYCLE_MAX_DEPTH = 12
LIFECYCLE_STATUSES = frozenset({
    "RUNNING", "DONE", "SUCCESS", "FAILED", "ERROR",
    "PENDING", "CANCELLED", "COMPLETED", "REQUESTED",
})


def _safe_lifecycle_status(value: object) -> str:
    return value if isinstance(value, str) and value in LIFECYCLE_STATUSES else "OTHER"


def _target_reference_location(params: object) -> str:
    """Location of a basename reference, never a verified file read.

    Inspect values and keys separately. A filename appearing as a JSON key
    must not be mistaken for a path value. Arbitrary unknown keys are never
    printed or trusted as canonical read_file path fields.
    """
    _, obj = _parameter_envelope(params)
    direct = False
    nested = False
    key_only = False

    def visit(node: object, depth: int) -> None:
        nonlocal direct, nested, key_only
        if depth > LIFECYCLE_MAX_DEPTH:
            return
        if isinstance(node, str):
            if LIFECYCLE_TARGET in node.casefold():
                if depth <= 1:
                    direct = True
                else:
                    nested = True
        elif isinstance(node, dict):
            for key, value in list(node.items())[:200]:
                if isinstance(key, str) and LIFECYCLE_TARGET in key.casefold():
                    key_only = True
                visit(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:200]:
                visit(item, depth + 1)

    visit(obj, 0)
    if direct:
        return "DIRECT_VALUE"
    if nested:
        return "NESTED_VALUE"
    if key_only:
        return "KEY_ONLY"
    return "NONE"


def _safe_lifecycle_event_kind(row: dict) -> str:
    event = row.get("event")
    if event == "result":
        return "TERMINAL_RESULT"
    if event == "tool_result":
        return "TOOL_RESULT_EVENT"
    if event != "step_update":
        return "OTHER_EVENT"
    step = row.get("step_update")
    if not isinstance(step, dict):
        return "STEP_UPDATE_OTHER"
    kind = step.get("step_type")
    if kind == "tool":
        return "STEP_UPDATE_TOOL"
    if kind == "tool_result":
        return "STEP_UPDATE_TOOL_RESULT"
    return "STEP_UPDATE_OTHER"


def _inspect_lifecycle_trace(trace: Path) -> dict:
    """Examine original AGY typed events without copying any raw content."""
    records = []
    malformed = 0
    with trace.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if isinstance(row, dict):
                records.append(row)

    read_events = 0
    events = []
    step_indexes = {}
    step_status_groups = {}
    step_statuses = set()
    call_ids = {}
    missing_ids = 0
    param_digests = set()
    strict_count = 0
    locations = set()
    event_positions = []
    for index, row in enumerate(records):
        if row.get("event") != "step_update":
            continue
        step = row.get("step_update")
        if not isinstance(step, dict) or step.get("step_type") != "tool":
            continue
        info = step.get("tool_info")
        if not isinstance(info, dict):
            continue
        name = info.get("name") or step.get("tool_name")
        if not isinstance(name, str) or name.split(".")[-1] != "read_file":
            continue
        read_events += 1
        params = info.get("parameters")
        if not _legacy_target_reference(params):
            continue
        events.append(index)
        event_positions.append(index)
        strict_count += int(_target_argument(params))
        locations.add(_target_reference_location(params))
        idx = step.get("step_index")
        if type(idx) is int:
            step_indexes[idx] = step_indexes.get(idx, 0) + 1
        # Status values are never copied verbatim unless allowlisted.
        state = _safe_lifecycle_status(step.get("status"))
        step_statuses.add(state)
        if type(idx) is int:
            step_status_groups.setdefault(idx, set()).add(state)
        identifier = _call_id(info, step)
        if identifier is None:
            missing_ids += 1
        else:
            call_ids[identifier] = call_ids.get(identifier, 0) + 1
        # Compare only in-memory digests of private serialized arguments.
        # Neither the value nor its digest is ever included in the report.
        try:
            serialized = json.dumps(
                params, sort_keys=True, ensure_ascii=False, default=str)
        except (ValueError, TypeError):
            serialized = "<UNSERIALIZABLE>"
        param_digests.add(hashlib.sha256(serialized.encode("utf-8")).digest())

    followups = set()
    for index in event_positions:
        for row in records[index + 1:index + 3]:
            followups.add(_safe_lifecycle_event_kind(row))

    linked = False
    for row in records:
        if row.get("event") == "tool_result" and row.get("source") == "TOOL":
            identity = row.get("tool_call_id") or row.get("call_id")
            if isinstance(identity, str) and identity in call_ids:
                linked = True
        if row.get("event") == "step_update":
            step = row.get("step_update")
            if (isinstance(step, dict)
                    and step.get("step_type") == "tool_result"
                    and step.get("source") == "TOOL"):
                identity = step.get("tool_call_id")
                if isinstance(identity, str) and identity in call_ids:
                    linked = True

    repeat_index_groups = sum(count > 1 for count in step_indexes.values())
    same_step_status_change = any(
        len(states - {"OTHER"}) > 1 for states in step_status_groups.values())
    duplicate_call_ids = any(count > 1 for count in call_ids.values())
    repeat_possible = len(events) > 1 and (
        bool(repeat_index_groups) or duplicate_call_ids
        or len(param_digests) < len(events))
    if not locations:
        location = "NONE"
    elif len(locations) == 1:
        location = next(iter(locations))
    else:
        location = "MIXED_UNTRUSTED_REFERENCES"
    return {
        "trace_status": "INCOMPLETE_TRACE" if malformed else "TRACE_PRESENT",
        "read_file_tool_event_count": read_events,
        "target_tool_event_count": len(events),
        "known_strict_target_count": strict_count,
        "target_reference_location": location,
        "distinct_step_index_count": len(step_indexes),
        "repeated_step_index_groups": repeat_index_groups,
        "distinct_call_id_count": len(call_ids),
        "missing_call_id_event_count": missing_ids,
        "distinct_argument_envelope_count": len(param_digests),
        "repeat_update_possible": repeat_possible,
        "tool_step_statuses": sorted(step_statuses),
        "same_step_status_change_observed": same_step_status_change,
        "verified_distinct_file_reads": False,
        "post_target_event_types": sorted(followups),
        "linked_tool_result_observed": linked,
        "correlation_level": (
            "EXPLICIT_TOOL_CALL_ID" if linked else
            "ADJACENCY_ONLY" if followups else "NO_CORRELATION"),
        "model_ingress_verified": False,
        "independent_binary_transport_verified": False,
    }


def inspect_lifecycle_session(private_root: Path, session: str) -> dict:
    """One-time, independent, read-only lifecycle metadata analysis."""
    priv, manifest = _read_manifest(private_root, session)
    trials = []
    for item in manifest["cases"]:
        trace = priv / (item["id"] + ".ndjson")
        row = {
            "trial": item["id"],
            "trace_status": "TRACE_MISSING",
            "read_file_tool_event_count": 0,
            "target_tool_event_count": 0,
            "known_strict_target_count": 0,
            "target_reference_location": "NONE",
            "distinct_step_index_count": 0,
            "repeated_step_index_groups": 0,
            "distinct_call_id_count": 0,
            "missing_call_id_event_count": 0,
            "distinct_argument_envelope_count": 0,
            "repeat_update_possible": False,
            "tool_step_statuses": [],
            "same_step_status_change_observed": False,
            "verified_distinct_file_reads": False,
            "post_target_event_types": [],
            "linked_tool_result_observed": False,
            "correlation_level": "NO_CORRELATION",
            "model_ingress_verified": False,
            "independent_binary_transport_verified": False,
        }
        if trace.is_symlink() or (trace.exists() and not _regular_file(
                trace, MAX_TRACE_BYTES)):
            row["trace_status"] = "UNSAFE_TRACE"
        elif trace.exists():
            row.update(_inspect_lifecycle_trace(trace))
        trials.append(row)
    return {
        "schema": "agy_cli_audio_lifecycle_v1",
        "session": session,
        "model_called": False,
        "private_evidence_modified": False,
        "independent_binary_transport_verified": False,
        "trials": trials,
        "note": ("Tool step updates and adjacent events cannot alone prove "
                 "distinct file reads or model-side binary ingestion."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline AGY MP3 tool-result byte audit (no model requests)")
    parser.add_argument("--session", required=True)
    parser.add_argument("--private-root", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inspect-shape", action="store_true",
        help="Inspect archived AGY read_file event envelopes without any private values")
    mode.add_argument(
        "--inspect-lifecycle", action="store_true",
        help="Summarize call-step repetitions and typed follow-up events without model calls")
    args = parser.parse_args(argv)
    if args.private_root is None:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            parser.error("Provide --private-root outside Windows")
        private_root = Path(local) / "AGYBlindAudio" / "truth"
    else:
        private_root = args.private_root
    try:
        report = (inspect_lifecycle_session(private_root, args.session)
                  if args.inspect_lifecycle else
                  probe_session(private_root, args.session)
                  if args.inspect_shape else
                  audit_session(private_root, args.session))
    except (ValueError, OSError):
        print(json.dumps({"schema": "agy_cli_audio_byte_audit_v1",
                          "status": "AUDIT_UNAVAILABLE_OR_INVALID_EVIDENCE",
                          "model_called": False}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
