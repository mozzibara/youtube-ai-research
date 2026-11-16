"""Redacted inspection of local AGY JSONL read_file tool events.

Only flags, step indices and tool names are reported. No private paths,
prompts, media data or model transcripts are copied into the output.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TOOL_NAMES = frozenset({"read_file", "read_media_file"})
RESULT_KEYS = ("tool_results", "tool_responses", "tool_outputs", "tool_result", "result")
ARG_KEYS = ("arguments", "args", "input", "parameters")
DATA_KEYS = frozenset({"data", "base64", "base64_data", "blob", "binary",
                      "bytes", "inline_data", "inlineData"})
MAX_DEPTH = 15


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""


def _tool_name(call: dict[str, Any]) -> str:
    for key in ("name", "tool_name"):
        val = call.get(key)
        if isinstance(val, str):
            return val.split(".")[-1]
    fn = call.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"].split(".")[-1]
    return ""


def _arguments(call: dict[str, Any]) -> Any:
    for key in ARG_KEYS:
        if key in call:
            return call[key]
    fn = call.get("function")
    return fn.get("arguments") if isinstance(fn, dict) else None


def _results(record: dict[str, Any], call: dict[str, Any]) -> list[Any]:
    out = []
    for obj in (call, record):
        for key in RESULT_KEYS:
            if key in obj and obj[key] is not None:
                out.append(obj[key])
    return out


def _walk(value: Any, depth: int = 0):
    if depth >= MAX_DEPTH:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k), v
            yield from _walk(v, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, depth + 1)


def _result_metadata(results: list[Any]) -> dict[str, bool]:
    # Even a MIME and payload marker do NOT prove actual audio reached the model.
    audio_mime_hint = False
    inline_payload_hint = False
    for item in results:
        for key, value in _walk(item):
            low = key.lower()
            if isinstance(value, str) and ("mime" in low or low in
                    {"type", "format", "media_type"}):
                audio_mime_hint |= value.lower().strip().startswith("audio/")
            if key in DATA_KEYS or low in DATA_KEYS:
                if isinstance(value, (str, bytes, list)) and len(value) > 100:
                    inline_payload_hint = True
    return {"audio_mime_hint": audio_mime_hint,
            "inline_payload_hint": inline_payload_hint}


# Fixed structural vocabulary; dynamic dictionary keys and string values are never emitted.
SCHEMA_FIELDS = frozenset({
    "step_index", "status", "tool_calls", "tool_results", "tool_responses",
    "tool_outputs", "tool_result", "result", "message", "messages",
    "content", "role", "type", "event_type", "output", "response",
    "tool_call_id", "tool_use_id", "name", "tool_name", "function",
    "arguments", "args", "input", "parameters", "parts", "inline_data",
    "inlineData", "mime_type", "mimeType", "data", "text",
    "file_path", "path", "success", "error",
})


def _shape(value: Any, depth: int = 0) -> dict[str, Any]:
    if depth >= 5:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, dict):
        selected = {key: _shape(item, depth + 1)
                    for key, item in value.items() if key in SCHEMA_FIELDS}
        # Never print unrecognized keys; they may contain user paths or identifiers.
        return {"type": "object", "fields": selected,
                "unrecognized_key_count": len(value) - len(selected)}
    if isinstance(value, list):
        return {"type": "array", "count": len(value),
                "first_element_shape": _shape(value[0], depth + 1)
                if value else None}
    return {"type": "null" if value is None else
            "boolean" if isinstance(value, bool) else
            "number" if isinstance(value, (int, float)) else
            "string" if isinstance(value, str) else "other"}


def _preview_schema(path: Path, target_lines: list[int]) -> list[dict[str, Any]]:
    """Return structure, never values, for records around a targeted tool call."""
    if not target_lines:
        return []
    first = target_lines[0]
    lower, upper = max(1, first - 1), first + 4
    records = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for i, line in enumerate(stream, 1):
            if i < lower:
                continue
            if i > upper:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                records.append({"line": i, "unparseable": True})
                continue
            if isinstance(record, dict):
                records.append({"line": i, "step": record.get("step_index")
                                if isinstance(record.get("step_index"), int) else None,
                                "structure": _shape(record)})
    return records


def summarize(path: Path, target: str = "clip_00_30.mp3",
              schema_preview: bool = False) -> dict[str, Any]:
    if not target or "/" in target or "\\" in target or target in {".", ".."}:
        raise ValueError("target must be a filename, not a path")
    calls = []
    invalid = 0
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if not isinstance(record, dict):
                continue
            raw_calls = record.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                continue
            for call in raw_calls:
                if not isinstance(call, dict) or _tool_name(call) not in TOOL_NAMES:
                    continue
                target_match = target.casefold() in _stringify(_arguments(call)).casefold()
                results = _results(record, call)
                step = record.get("step_index")
                step = step if isinstance(step, int) else None
                status = record.get("status")
                status = status if status in {"DONE", "FAILED", "ERROR", "SUCCESS", "PENDING"} else "UNKNOWN"
                calls.append({
                    "step": step, "line": line_number, "tool": _tool_name(call),
                    "status": status, "target_in_call_arguments": target_match,
                    "result_record_present": bool(results),
                    **_result_metadata(results),
                })
    target_count = sum(1 for c in calls if c["target_in_call_arguments"])
    report = {
        "schema": "agy_read_file_trace_metadata_v1",
        "target_basename": target,
        "read_file_calls": len(calls),
        "target_file_calls": target_count,
        "calls": calls,
        "jsonl_invalid_lines": invalid,
        "independent_audio_receipt_verified": False,
        "assessment": ("TARGET_READ_CALL_OBSERVED_AUDIO_UNVERIFIED" if target_count
                       else "READ_CALLS_OBSERVED_TARGET_UNVERIFIED" if calls
                       else "NO_READ_FILE_CALL_OBSERVED"),
        "limitations": "Metadata only: no audio playback or independent model payload verification.",
    }
    if schema_preview:
        target_lines = [c["line"] for c in calls if c["target_in_call_arguments"]]
        report["schema_preview"] = _preview_schema(path, target_lines)
        report["schema_preview_note"] = (
            "Field names and value types only; no tool returns or audio bytes shown. "
            "Nearby records are not verified to be tool-result events."
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline, redacted AGY read_file trace inspector")
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--target", default="clip_00_30.mp3")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema-preview", action="store_true",
                        help="Print only field names/types for target call and next records")
    args = parser.parse_args(argv)
    report = json.dumps(summarize(args.trace, args.target, args.schema_preview),
                        ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
