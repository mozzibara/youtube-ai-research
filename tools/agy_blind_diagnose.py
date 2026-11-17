"""Offline metadata-only analysis of empty AGY headless trial responses.

Reads archived private logs but NEVER prints raw paths, text, model output,
audio, stderr, tool arguments, or reference phrases. No AGY/model calls.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from typing import Any

SESSION_RE = re.compile(r"blind-[0-9a-f]{16}\Z")
CASE_RE = re.compile(r"case-[0-9a-f]{12}\Z")
DENIAL = re.compile(
    r"permission denied|tool (?:was )?denied|(?:soft|auto)[ -]?denied|"
    r"requires? (?:user )?approval|operation (?:was )?denied|"
    r"not authorized|access denied|not permitted|"
    r"(?:tool|permission) (?:is )?not allowed",
    re.IGNORECASE,
)
# These categories are evidence labels, not a claim about the root cause.
# Never output matched excerpts, full lines, or unknown/dynamic regex groups.
PERMISSION_SIGNATURES = (
    ("HEADLESS_AUTO_DENIAL", re.compile(
        r"auto[ -]?denied|headless.{0,120}cannot prompt|"
        r"permission.{0,120}headless.{0,120}prompt",
        re.IGNORECASE | re.DOTALL)),
    ("EXPLICIT_USER_DENY_RULE", re.compile(
        r"user[- ]configured deny rule|matches.{0,100}deny rule|"
        r"explicit.{0,80}deny rule", re.IGNORECASE | re.DOTALL)),
    ("SANDBOX_OR_APPCONTAINER_RESTRICTION", re.compile(
        r"appcontainer.{0,100}(?:denied|blocked|forbidden)|"
        r"sandbox.{0,100}(?:denied|blocked|not permitted)|"
        r"(?:denied|blocked).{0,100}(?:sandbox|appcontainer)",
        re.IGNORECASE | re.DOTALL)),
    ("FILESYSTEM_ACCESS_DENIED", re.compile(
        r"(?:EACCES|EPERM)\b|access is denied|"
        r"(?:filesystem|file system).{0,80}permission denied",
        re.IGNORECASE)),
    ("GENERIC_PERMISSION_DENIAL", DENIAL),
)
KNOWN_TOOL_MENTIONS = frozenset({"read_file", "write_file", "command", "list_dir"})
KNOWN_RESPONSE_PAYLOAD_KEYS = frozenset({
    "text_delta", "text", "content", "message", "response", "output",
})


def classify_stderr(raw: str) -> dict:
    """Return allowlisted presence bits only; nothing from stderr is echoed."""
    categories = [name for name, pattern in PERMISSION_SIGNATURES
                  if pattern.search(raw)]
    # More specific categories take precedence over generic DENIAL.
    signature = next((cat for cat in categories
                      if cat != "GENERIC_PERMISSION_DENIAL"),
                     "GENERIC_PERMISSION_DENIAL" if categories else "NO_DENIAL_PATTERN")
    required_match = re.search(
        r"tool required the [\"']([a-z_]+)[\"'] permission",
        raw, re.IGNORECASE
    )
    required_kind = (required_match.group(1).lower()
                     if required_match else "")
    required_kind = (required_kind
                     if required_kind in KNOWN_TOOL_MENTIONS
                     else "UNKNOWN_OR_NOT_STATED")
    mentioned = sorted(tool for tool in KNOWN_TOOL_MENTIONS
                       if re.search(r"(?<![a-z_])" + re.escape(tool) +
                                    r"(?![a-z_])", raw, re.IGNORECASE))
    return {
        "signature": signature,
        "headless_required_permission_kind": required_kind,
        "matched_categories": categories,
        "known_tools_mentioned": mentioned,
        "stderr_has_auth_or_quota_hint": bool(re.search(
            r"RESOURCE_EXHAUSTED|quota|429|UNAUTHENTICATED|"
            r"token expired|login required", raw, re.IGNORECASE)),
        "stderr_character_count": len(raw),
    }


TOOL_STATES = frozenset({"ACTIVE", "DONE", "FAILED", "ERROR", "CANCELED"})
TERMINAL_STATES = frozenset({"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED",
                              "INVALID", "WAITING", "RUNNING"})
KNOWN_EVENTS = frozenset({"init", "step_update", "result", "command_result",
                           "heartbeat"})
MAX_LOG_BYTES = 25 * 1024 * 1024


def _safe_state(state: Any, values: frozenset[str]) -> str:
    return state if isinstance(state, str) and state in values else "OTHER"


def _text_info(value: Any) -> tuple[bool, int, bool]:
    if not isinstance(value, str):
        return False, 0, False
    return True, len(value), bool(DENIAL.search(value))


def analyze_trace(trace: Path, stderr_path: Path) -> dict:
    if trace.is_symlink() or (trace.exists() and trace.stat().st_size > MAX_LOG_BYTES):
        raise ValueError("Refusing symlink or oversized private trace")
    raw_stderr = ""
    if stderr_path.exists():
        if stderr_path.is_symlink() or stderr_path.stat().st_size > MAX_LOG_BYTES:
            raise ValueError("Refusing symlink or oversized stderr")
        raw_stderr = stderr_path.read_text(encoding="utf-8-sig", errors="replace")
    stderr_summary = classify_stderr(raw_stderr)
    denied_stderr = stderr_summary["signature"] != "NO_DENIAL_PATTERN"
    if not trace.exists():
        return {"trace_present": False, "stderr_permission_denial_hint": denied_stderr,
                "stderr_permission_fingerprint": stderr_summary,
                "diagnosis": "TRACE_MISSING"}
    event_counts = {event: 0 for event in sorted(KNOWN_EVENTS)}
    unknown_event_count = 0
    invalid_lines = 0
    terminal_statuses: list[str] = []
    terminal_response_sizes: list[int] = []
    final_model_delta_chunks: list[str] = []
    model_response_steps = set()
    tool_steps: list[dict[str, Any]] = []
    agent_response_key_counts = {
        key: 0 for key in sorted(KNOWN_RESPONSE_PAYLOAD_KEYS)
    }
    with trace.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                invalid_lines += 1
                continue
            if not isinstance(row, dict):
                invalid_lines += 1
                continue
            event = row.get("event")
            if event in event_counts:
                event_counts[event] += 1
            else:
                unknown_event_count += 1
            if event == "result":
                result = row.get("result")
                if isinstance(result, dict):
                    terminal_statuses.append(_safe_state(
                        result.get("status"), TERMINAL_STATES))
                    response = result.get("response")
                    terminal_response_sizes.append(
                        len(response) if isinstance(response, str) else 0)
                continue
            if event != "step_update":
                continue
            step = row.get("step_update")
            if not isinstance(step, dict):
                continue
            if step.get("step_type") == "agent_response":
                for known in agent_response_key_counts:
                    if known in step:
                        agent_response_key_counts[known] += 1
                delta = step.get("text_delta")
                # Only count original fragments. Never expose transcript text.
                if isinstance(delta, str):
                    final_model_delta_chunks.append(delta)
                step_no = step.get("step_index")
                if type(step_no) is int:
                    model_response_steps.add(step_no)
                continue
            if step.get("step_type") != "tool":
                continue
            info = step.get("tool_info")
            if not isinstance(info, dict):
                continue
            raw_name = info.get("name", step.get("tool_name"))
            name = raw_name.split(".")[-1] if isinstance(raw_name, str) else "OTHER"
            name = name if name in {"read_file", "write_file", "write_to_file"} else "OTHER"
            parameters = info.get("parameters")
            # Never leak argument contents or absolute source paths.
            target = (name == "read_file" and isinstance(parameters, (str, dict))
                      and "clip_00_30.mp3" in json.dumps(parameters,
                                                         ensure_ascii=False).lower())
            step_state = _safe_state(step.get("state"), TOOL_STATES)
            output = info.get("output")
            output_present, output_chars, denial_hint = _text_info(output)
            tool_steps.append({
                "tool": name,
                "state": step_state,
                "target_mp3_in_parameters": bool(target),
                "tool_output_string_present": output_present,
                "tool_output_chars": output_chars,
                "tool_output_denial_hint": denial_hint,
                "tool_error_present": bool(info.get("error")),
            })
    # Some AGY releases send ACTIVE and DONE updates for the same step.
    mp3_calls = [x for x in tool_steps if x["target_mp3_in_parameters"]]
    denied_tool = any(x["tool_output_denial_hint"] or x["tool_error_present"]
                      for x in mp3_calls)
    has_delta = bool("".join(final_model_delta_chunks).strip())
    terminal_empty = bool(terminal_response_sizes) and not any(terminal_response_sizes)
    if denied_stderr or denied_tool:
        diagnosis = "POSSIBLE_TOOL_PERMISSION_DENIAL_OR_ERROR"
    elif not mp3_calls:
        diagnosis = "NO_TARGET_MP3_READ_ATTEMPT"
    elif not terminal_statuses:
        diagnosis = "NO_TERMINAL_RESULT"
    elif terminal_empty and has_delta:
        diagnosis = "EMPTY_TERMINAL_RESPONSE_AGENT_TEXT_PRESENT"
    elif terminal_empty:
        diagnosis = "SUCCESS_OR_OTHER_TERMINAL_WITH_NO_MODEL_TEXT"
    else:
        diagnosis = "TERMINAL_HAS_RESPONSE_BUT_OUTPUT_NOT_CAPTURED"
    return {
        "trace_present": True,
        "event_counts": event_counts,
        "unknown_event_count": unknown_event_count,
        "invalid_lines": invalid_lines,
        "terminal_result_count": len(terminal_statuses),
        "terminal_statuses": terminal_statuses,
        "terminal_response_character_counts": terminal_response_sizes,
        "agent_response_step_count": len(model_response_steps),
        "agent_response_text_delta_count": len(final_model_delta_chunks),
        "agent_response_known_payload_key_counts": {
            key: count for key, count in agent_response_key_counts.items()
            if count > 0
        },
        "agent_response_text_character_count": sum(map(len, final_model_delta_chunks)),
        "target_mp3_read_step_updates": len(mp3_calls),
        "target_tool_state_counts": {
            state: sum(x["state"] == state for x in mp3_calls)
            for state in sorted(TOOL_STATES) if any(
                x["state"] == state for x in mp3_calls)
        },
        "target_tool_output_string_seen": any(x["tool_output_string_present"]
                                               for x in mp3_calls),
        "target_tool_output_character_count": sum(
            x["tool_output_chars"] for x in mp3_calls),
        "target_tool_denial_or_error_hint": denied_tool,
        "stderr_permission_denial_hint": denied_stderr,
        "stderr_permission_fingerprint": stderr_summary,
        "diagnosis": diagnosis,
        "audio_bytes_reached_model_verified": False,
    }


def summarize(private_root: Path, session: str) -> dict:
    if not SESSION_RE.fullmatch(session):
        raise ValueError("Invalid session")
    base = private_root.resolve() / session
    manifest_file = base / "manifest.json"
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise FileNotFoundError("Private manifest unavailable")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("session") != session or manifest.get("schema") != "agy_blind_audio_v1":
        raise ValueError("Unexpected private manifest")
    cases = manifest.get("cases", [])
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise ValueError("Invalid manifest case count")
    outputs = []
    for item in cases:
        cid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(cid, str) or not CASE_RE.fullmatch(cid):
            raise ValueError("Unsafe case id")
        info = analyze_trace(base / (cid + ".ndjson"), base / (cid + ".stderr"))
        outputs.append({"trial": cid, **info})
    return {"schema": "agy_blind_empty_response_diagnostic_v1",
            "session": session, "model_called": False, "case_count": len(outputs),
            "trials": outputs, "private_text_exposed": False,
            "note": "Trace classification only. MP3 read attempts and CLI SUCCESS do not prove audio reception."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline/redacted AGY empty-answer diagnostics")
    parser.add_argument("--session", required=True)
    parser.add_argument("--private-root", type=Path)
    args = parser.parse_args(argv)
    root = args.private_root
    if root is None:
        home = os.environ.get("LOCALAPPDATA")
        if not home:
            parser.error("LOCALAPPDATA missing; pass --private-root")
        root = Path(home) / "AGYBlindAudio" / "truth"
    print(json.dumps(summarize(root, args.session), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
