"""Local-only, lossless Antigravity response archives with privacy-safe summaries.

Raw responses may contain transcripts, account verification links, and other
secrets. The entire directory is under data/ (gitignored); never publish it.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")


def _safe_name(value, default):
    value = str(value or "")
    return value if value not in (".", "..") and _SAFE_SEGMENT.fullmatch(value) else default


def _shape_of_result(stdout):
    """Read counts and known schema keys only; do not expose response text."""
    summary = {"stdout_chars": len(stdout), "events": {}, "final_status": "unavailable"}
    try:
        document = json.loads(stdout)
        if isinstance(document, dict) and "event" not in document:
            final = document
            summary["envelope_format"] = "json"
        elif isinstance(document, dict) and document.get("event") == "result":
            final = document.get("result")
            summary["envelope_format"] = "single_event"
        else:
            final = None
    except (TypeError, ValueError):
        final = None
    if final is None:
        summary["envelope_format"] = "stream-json"
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                summary["unparseable_event_lines"] = summary.get("unparseable_event_lines", 0) + 1
                continue
            event_name = event.get("event") if isinstance(event, dict) else None
            if event_name not in ("init", "step_update", "result"):
                event_name = "unknown"
            summary["events"][event_name] = summary["events"].get(event_name, 0) + 1
            if event_name == "result":
                final = event.get("result")
    if not isinstance(final, dict):
        return summary
    status = final.get("status")
    summary["final_status"] = status if status in ("SUCCESS", "ERROR") else "other"
    structured = final.get("structured_output")
    summary["structured_output_type"] = type(structured).__name__
    response = final.get("response")
    summary["response_chars"] = len(response) if isinstance(response, str) else 0
    output = structured if isinstance(structured, dict) else None
    if output is None and isinstance(response, str):
        try:
            output = json.loads(response)
        except ValueError:
            output = None
    summary["schema_fields"] = {
        name: {
            "present": isinstance(output, dict) and name in output,
            "type": type(output[name]).__name__ if isinstance(output, dict) and name in output else None,
            "items": len(output[name]) if isinstance(output, dict) and isinstance(output.get(name), list) else None,
        }
        for name in ("claims", "sections", "issues")
    }
    return summary


def archive_agy_attempt(root: Path, *, role: str, model: str, attempt: int,
                        context: dict | None, stdout: str, stderr: str,
                        exit_code: int | None, error: BaseException | None = None,
                        duration_seconds: float | None = None, prompt: str = "") -> Path:
    """Create a new private folder per invocation; no overwrite and no prompt text."""
    context = context or {}
    video_id = _safe_name(context.get("video_id"), "unknown-video")
    unit = _safe_name(context.get("unit"), "unknown-unit")
    role = _safe_name(role, "unknown-role")
    model = _safe_name(model, "unknown-model")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = root / "data" / "diagnostics" / "agy" / video_id / role
    folder = base / f"{stamp}_{unit}_try{attempt}_{uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    # Exact raw bytes of the *decoded UTF-8 subprocess stream* are preserved.
    # No JSON normalization, truncation, prompt, command, or authentication token in metadata.
    for name, content in (("stdout.raw.txt", stdout), ("stderr.raw.txt", stderr)):
        with (folder / name).open("x", encoding="utf-8", newline="") as target:
            target.write(content or "")
    error_message = str(error) if error else ""
    code_match = re.search(r"\b[A-Z][A-Z0-9_]{3,80}\b", error_message)
    error_code = code_match.group(0) if code_match and _SAFE_ERROR.fullmatch(code_match.group(0)) else None
    summary = _shape_of_result(stdout or "")
    summary.update({
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "video_id": video_id,
        "unit": unit,
        "role": role,
        "model": model,
        "attempt": attempt,
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else None,
        "outcome": "adapter_failed" if error else "adapter_parsed",
        "error_type": type(error).__name__ if error else None,
        "error_code": error_code,
        "stdout_sha256": hashlib.sha256((stdout or "").encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256((stderr or "").encode("utf-8")).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "raw_files_private": True,
        "note": "adapter_parsed does NOT mean the extraction passed later pipeline validation.",
    })
    with (folder / "summary.json").open("x", encoding="utf-8") as target:
        json.dump(summary, target, ensure_ascii=False, indent=2)
        target.write("\n")
    return folder


def list_diagnostic_summaries(root: Path, *, video_id: str | None = None, limit: int = 20):
    base = root / "data" / "diagnostics" / "agy"
    if video_id:
        base /= _safe_name(video_id, "unknown-video")
    if not base.exists():
        return []
    paths = sorted(base.rglob("summary.json"), reverse=True)
    result = []
    for path in paths[:max(0, limit)]:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        result.append((path, item))
    return result


def inspect_saved_response_structure(folder: Path):
    """Inspect locally archived stdout without returning any model text values.

    Top-level keys are returned only for explicitly requested local inspection.
    Export-safe summaries do not include these labels. Never follow symlinks.
    """
    original = folder / "stdout.raw.txt"
    if original.is_symlink() or not original.is_file():
        raise FileNotFoundError("로컬 원본 stdout 파일이 없습니다")
    raw = original.read_text(encoding="utf-8")
    try:
        root = json.loads(raw)
        if isinstance(root, dict) and root.get("event") == "result":
            envelope = root.get("result")
        else:
            envelope = root
    except ValueError:
        envelope = None
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "result":
                envelope = event.get("result")
    if not isinstance(envelope, dict):
        return {"error": "최종 result JSON 객체를 찾지 못함"}
    response = envelope.get("response")
    if not isinstance(response, str):
        return {"error": "최종 result에 문자열 response가 없음"}
    decoder = json.JSONDecoder()
    pos, documents = 0, []
    tail = None
    while pos < len(response) and len(documents) < 32:
        while pos < len(response) and response[pos].isspace():
            pos += 1
        if pos >= len(response):
            break
        try:
            obj, end = decoder.raw_decode(response, pos)
        except ValueError:
            tail = response[pos:]
            break
        documents.append(obj)
        pos = end
    if len(documents) == 32 and pos < len(response):
        tail = response[pos:]

    def safe_label(key):
        # Show short Korean/Latin category headings locally; redact possible
        # emails, URLs, token strings, long/private freeform text.
        if not isinstance(key, str) or len(key) > 64:
            return "<redacted-key>"
        if "@" in key or "://" in key or "\n" in key or "\r" in key:
            return "<redacted-key>"
        if re.search(r"(?:token|password|secret|apikey|api_key)", key, re.I):
            return "<redacted-key>"
        if not all(ch.isalnum() or ch in " _-·/()." for ch in key):
            return "<redacted-key>"
        return key

    entries = []
    for index, document in enumerate(documents, 1):
        if not isinstance(document, dict):
            entries.append({"document": index, "root_type": type(document).__name__})
            continue
        fields = []
        for key, value in list(document.items())[:24]:
            info = {"key": safe_label(key), "type": type(value).__name__}
            if isinstance(value, list):
                info["count"] = len(value)
                if value:
                    first = value[0]
                    info["item_type"] = type(first).__name__
                    if isinstance(first, dict):
                        info["item_keys"] = [safe_label(x) for x in list(first)[:12]]
                        info["item_field_types"] = [
                            {"name": safe_label(k), "type": type(v).__name__}
                            for k, v in list(first.items())[:12]
                        ]
            elif isinstance(value, dict):
                # Only metadata: never emit values from the model response.
                info["field_count"] = len(value)
                info["fields"] = [
                    {"name": safe_label(k), "type": type(v).__name__,
                     "count": len(v) if isinstance(v, (list, dict)) else None}
                    for k, v in list(value.items())[:16]
                ]
            elif isinstance(value, str):
                # Distinguish serialized JSON arrays from free-form strings
                # without exposing a single character of model content.
                info["string_chars"] = len(value)
                if not value.strip():
                    info["string_kind"] = "blank"
                else:
                    try:
                        decoded_value = json.loads(value)
                    except ValueError:
                        info["string_kind"] = "not_json"
                    else:
                        kind = type(decoded_value).__name__
                        info["string_kind"] = f"json_{kind}"
                        if isinstance(decoded_value, (list, dict)):
                            info["decoded_items"] = len(decoded_value)
                        if isinstance(decoded_value, list):
                            info["decoded_first_type"] = (
                                type(decoded_value[0]).__name__ if decoded_value else "none"
                            )
                            if decoded_value and isinstance(decoded_value[0], dict):
                                info["decoded_first_item_keys"] = [
                                    safe_label(k) for k in list(decoded_value[0])[:12]
                                ]
            fields.append(info)
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        entries.append({
            "document": index,
            "fields": fields,
            "content_fingerprint": digest,
        })
    return {
        "documents_decoded": len(documents),
        "documents_identical": (
            len(entries) > 1 and len({e.get("content_fingerprint") for e in entries}) == 1
        ),
        "trailing_unparsed_chars": len(tail) if tail is not None else 0,
        "documents": entries,
        "warning": "키 이름만 로컬 출력. 원문 stdout 및 stderr는 공유하지 마세요.",
    }




def inspect_saved_document_summary(folder: Path):
    """Compact metadata-only overview of mixed JSON documents.

    Intended for copying into bug reports: no string values, claim text,
    transcript excerpts, authentication links or raw segment IDs are included.
    """
    detailed = inspect_saved_response_structure(folder)
    result = {
        "documents_decoded": detailed.get("documents_decoded", 0),
        "documents_identical": detailed.get("documents_identical", False),
        "trailing_unparsed_chars": detailed.get("trailing_unparsed_chars", 0),
        "documents": [],
    }
    for document in detailed.get("documents", []):
        fields = document.get("fields")
        if not isinstance(fields, list):
            result["documents"].append({
                "document": document.get("document"),
                "root_type": document.get("root_type", "unknown"),
            })
            continue
        types, string_kinds = {}, {}
        evidence_records = 0
        embedded_json_records = 0
        unparsed_string_fields = []
        for field in fields:
            field_type = field.get("type", "unknown")
            types[field_type] = types.get(field_type, 0) + 1
            if field_type == "list":
                if {"claim", "segment_ids"}.issubset(field.get("item_keys", [])):
                    evidence_records += field.get("count", 0)
            elif field_type == "str":
                kind = field.get("string_kind", "unknown")
                string_kinds[kind] = string_kinds.get(kind, 0) + 1
                if kind == "json_list" and {
                    "claim", "segment_ids"
                }.issubset(field.get("decoded_first_item_keys", [])):
                    embedded_json_records += field.get("decoded_items", 0)
                if kind == "not_json":
                    unparsed_string_fields.append({
                        "name": field.get("key", "<redacted-key>"),
                        "chars": field.get("string_chars", 0),
                    })
        result["documents"].append({
            "document": document.get("document"),
            "root_field_count": len(fields),
            "field_types": types,
            "list_evidence_records": evidence_records,
            "embedded_json_evidence_records": embedded_json_records,
            "string_kinds": string_kinds,
            "unparsed_string_fields": unparsed_string_fields,
            "content_fingerprint": document.get("content_fingerprint"),
        })
    result["warning"] = (
        "이 결과는 모델 내용을 표시하지 않는 구조 진단입니다. "
        "문자열 형식 분석이 손실 없이 복구 가능한지 추가 검증이 필요합니다."
    )
    return result


def find_matching_agy_archive(root: Path, *, role: str, model: str, prompt: str, context: dict):
    """Find archived CLI output for exactly this model, role, chunk and prompt.

    No loose video-only lookup: a changed transcript/prompt must not replay
    an unrelated run. Verify raw stdout digest before returning the text.
    """
    video_id = _safe_name(context.get("video_id"), "unknown-video")
    unit = _safe_name(context.get("unit"), "unknown-unit")
    target = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    base = root / "data" / "diagnostics" / "agy" / video_id / _safe_name(role, "unknown-role")
    if not base.is_dir():
        return None
    for folder in sorted(base.iterdir(), reverse=True):
        if not folder.is_dir() or folder.is_symlink():
            continue
        metadata_path = folder / "summary.json"
        raw_path = folder / "stdout.raw.txt"
        stderr_path = folder / "stderr.raw.txt"
        if any(path.is_symlink() for path in (metadata_path, raw_path, stderr_path)):
            continue
        if not all(path.is_file() for path in (metadata_path, raw_path, stderr_path)):
            continue
        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not (meta.get("video_id") == video_id and meta.get("unit") == unit
                and meta.get("role") == role and meta.get("model") == model
                and meta.get("prompt_sha256") == target and meta.get("exit_code") == 0):
            continue
        raw = raw_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta.get("stdout_sha256"):
            raise RuntimeError("AGY_ARCHIVE_TAMPERED: archived stdout hash mismatch")
        stderr = stderr_path.read_bytes()
        if hashlib.sha256(stderr).hexdigest() != meta.get("stderr_sha256"):
            raise RuntimeError("AGY_ARCHIVE_TAMPERED: archived stderr hash mismatch")
        return folder, raw.decode("utf-8"), stderr.decode("utf-8")
    return None


def inspect_saved_claim_sections(folder: Path):
    """Summarize section *types and bounded numeric values* from raw model data.

    No claim text, transcript text, raw stderr or arbitrary model strings leave
    this function. Works on both streaming and non-streaming CLI archives.
    """
    path = folder / "stdout.raw.txt"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("원본 stdout 파일을 찾지 못했습니다")
    raw = path.read_text(encoding="utf-8")
    terminal = None
    try:
        root = json.loads(raw)
        if isinstance(root, dict):
            terminal = root.get("result") if root.get("event") == "result" else root
    except ValueError:
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "result":
                terminal = event.get("result")
    if not isinstance(terminal, dict):
        return {"error": "terminal result unavailable"}

    structured = terminal.get("structured_output")
    documents = [structured] if isinstance(structured, dict) else []
    if not documents:
        response = terminal.get("response")
        if not isinstance(response, str):
            return {"error": "response not a string"}
        decoder, pos = json.JSONDecoder(), 0
        while pos < len(response) and len(documents) < 32:
            while pos < len(response) and response[pos].isspace():
                pos += 1
            if pos >= len(response):
                break
            try:
                item, end = decoder.raw_decode(response, pos)
            except ValueError:
                break
            documents.append(item)
            pos = end

    from .definitions import SECTIONS
    headings = {f"{i}. {name}": i for i, name in enumerate(SECTIONS[:18], 1)}

    def numeric_label(value):
        if isinstance(value, bool):
            return "bool", None
        if isinstance(value, int):
            return "int", value if 0 <= value <= 99 else "<out-of-display-range>"
        if isinstance(value, str):
            trimmed = value.strip()
            if re.fullmatch(r"\d{1,2}", trimmed):
                return "numeric_string", trimmed
            if trimmed in headings:
                return "canonical_heading", headings[trimmed]
            return "other_string", None
        return type(value).__name__, None

    result = []
    for n, document in enumerate(documents[:8], 1):
        if not isinstance(document, dict):
            result.append({"document": n, "root_type": type(document).__name__})
            continue
        claims = document.get("claims")
        if not isinstance(claims, list):
            result.append({
                "document": n,
                "claims_type": type(claims).__name__,
                "root_field_count": len(document),
            })
            continue
        counts, invalid = {}, []
        section_missing = 0
        section_null = 0
        field_patterns = {}
        alternate_fields = {}
        category_ids = {}
        category_name_matches = 0
        category_name_mismatches = 0
        for index, claim in enumerate(claims, 1):
            if isinstance(claim, dict):
                if "category_id" in claim:
                    value_id = claim["category_id"]
                    safe_id = (
                        value_id if type(value_id) is int and 0 <= value_id <= 99
                        else "<invalid-or-out-of-range>"
                    )
                    category_ids[str(safe_id)] = category_ids.get(str(safe_id), 0) + 1
                    official_label = (
                        SECTIONS[value_id - 1]
                        if type(value_id) is int and 1 <= value_id <= 18 else None
                    )
                    given_name = claim.get("category_name")
                    if (official_label is not None and isinstance(given_name, str)
                            and given_name.strip() in (
                                official_label, f"{value_id}. {official_label}"
                            )):
                        category_name_matches += 1
                    else:
                        category_name_mismatches += 1
                if "section" not in claim:
                    section_missing += 1
                elif claim["section"] is None:
                    section_null += 1
                # Report only field names and types, never user/model values.
                safe_fields = []
                for field, value in claim.items():
                    label = str(field) if isinstance(field, str) else "<redacted-key>"
                    if (len(label) > 40 or not re.fullmatch(r"[A-Za-z0-9_가-힣-]+", label)
                            or re.search(r"token|secret|password|api_key", label, re.I)):
                        label = "<redacted-key>"
                    safe_fields.append((label, type(value).__name__))
                    if label in ("category", "category_name", "section_name", "topic", "분류", "섹션"):
                        alternate_fields[label] = type(value).__name__
                fingerprint = tuple(sorted(safe_fields))
                field_patterns[fingerprint] = field_patterns.get(fingerprint, 0) + 1
            if not isinstance(claim, dict):
                kind, value = type(claim).__name__, None
                acceptable = False
            else:
                original = claim.get("section")
                kind, value = numeric_label(original)
                acceptable = (
                    kind == "int" and isinstance(value, int) and 1 <= value <= 18
                ) or (
                    kind == "numeric_string" and value is not None and 1 <= int(value) <= 18
                ) or (
                    kind == "canonical_heading"
                )
            key = f"{kind}:{value}" if value is not None else kind
            counts[key] = counts.get(key, 0) + 1
            if not acceptable:
                invalid.append({"claim_index": index, "section_type": kind, "safe_value": value})
        result.append({
            "document": n,
            "claim_count": len(claims),
            "section_missing_count": section_missing,
            "section_null_count": section_null,
            "category_ids": category_ids,
            "category_name_matches": category_name_matches,
            "category_name_mismatches": category_name_mismatches,
            "claim_field_patterns": [
                {"fields": [{"name": key, "type": typ} for key, typ in signature],
                 "count": frequency}
                for signature, frequency in field_patterns.items()
            ],
            "alternate_section_field_types": alternate_fields,
            "section_histogram": counts,
            "invalid_sections": invalid[:20],
            "invalid_total": len(invalid),
        })
    return {
        "documents_seen": len(documents),
        "documents": result,
        "warning": "숫자·자료형만 표시. 분석 본문과 모델 응답 원본은 출력하지 않습니다.",
    }


_VIDEO_TOOL_SAFE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}$")
_VIDEO_ACCESS_TOOLS = ("read_url", "browser", "navigate")
_PERMISSION_DIAGNOSTIC = re.compile(
    r"(?i)(permission.{0,60}(?:denied|required|approval|ask)|"
    r"(?:soft[- ]?denied|approval required|not allowed|access denied))"
)


def inspect_video_access_archive(root: Path, video_id: str):
    """Safely classify the latest archived AGY video attempt, without AI calls.

    No prompt text, response text, tool parameters/output, URLs, stderr or
    authentication links are ever returned. Tool steps are *web* evidence,
    never proof that video frames or audio were decoded.
    """
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("VIDEO_DIAGNOSTIC_ID_INVALID")
    base = root / "data" / "diagnostics" / "agy" / video_id / "video_source"
    if base.is_symlink() or not base.is_dir():
        return {"found": False, "reason": "NO_ARCHIVED_VIDEO_ATTEMPT"}
    candidates = sorted(base.glob("*/summary.json"), reverse=True)
    for summary_file in candidates:
        folder = summary_file.parent
        stdout_file, stderr_file = folder / "stdout.raw.txt", folder / "stderr.raw.txt"
        if (folder.is_symlink() or summary_file.is_symlink()
                or stdout_file.is_symlink() or stderr_file.is_symlink()
                or not stdout_file.is_file() or not stderr_file.is_file()):
            continue
        stdout = stdout_file.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_file.read_text(encoding="utf-8", errors="replace")
        result = {
            "found": True,
            "video_id": video_id,
            "attempt_folder": folder.name,
            "init_seen": False,
            "agent_selection": "not_reported",
            "permission_mode": "not_reported",
            "step_type_counts": {},
            "available_web_tools": [],
            "available_tool_count": 0,
            "tool_steps": [],
            "tool_done_count": 0,
            "tool_target_match_count": 0,
            "successful_target_web_steps": 0,
            "tool_error_count": 0,
            "permission_hint": bool(_PERMISSION_DIAGNOSTIC.search(stderr)),
            "source_access_failed_response": False,
            "result_status": "unknown",
            "invalid_event_count": 0,
            "access_level": "not_observed",
        }
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                result["invalid_event_count"] += 1
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "init":
                result["init_seen"] = True
                init = event.get("init")
                init = init if isinstance(init, dict) else {}
                agent = init.get("agent")
                if isinstance(agent, dict):
                    agent = agent.get("name")
                if isinstance(agent, str) and _VIDEO_TOOL_SAFE_NAME.fullmatch(agent):
                    expected = (
                        "yt-research-source-probe"
                        if folder.name.find("_tool_probe_") >= 0
                        else "yt-research-video"
                    )
                    result["agent_selection"] = (
                        "matched" if agent == expected else "different"
                    )
                permission_mode = init.get("permission_mode")
                if permission_mode in (
                    "request-review", "always-proceed", "auto",
                    "strict", "accept-edits", "default",
                ):
                    result["permission_mode"] = permission_mode
                available = init.get("tools")
                if isinstance(available, list):
                    result["available_tool_count"] = len(available)
                    result["available_web_tools"] = sorted({
                        t for t in available
                        if isinstance(t, str)
                        and _VIDEO_TOOL_SAFE_NAME.fullmatch(t)
                        and any(k in t.lower() for k in _VIDEO_ACCESS_TOOLS)
                    })
            elif event.get("event") == "result":
                final = event.get("result")
                final = final if isinstance(final, dict) else {}
                status = final.get("status")
                result["result_status"] = (
                    status if status in ("SUCCESS", "ERROR", "CANCELED",
                                        "INTERRUPTED", "INVALID", "WAITING", "RUNNING")
                    else "other"
                )
                response = final.get("response")
                result["source_access_failed_response"] = (
                    isinstance(response, str)
                    and response.lstrip().startswith("SOURCE_ACCESS_FAILED")
                )
            elif event.get("event") == "step_update":
                step = event.get("step_update")
                if not isinstance(step, dict):
                    continue
                step_type = step.get("step_type")
                safe_type = (
                    step_type if step_type in (
                        "user_input", "agent_response", "tool", "checkpoint",
                    ) else "other"
                )
                result["step_type_counts"][safe_type] = (
                    result["step_type_counts"].get(safe_type, 0) + 1
                )
                if step_type != "tool":
                    continue
                info = step.get("tool_info")
                info = info if isinstance(info, dict) else {}
                name = step.get("tool_name") or info.get("name")
                if not isinstance(name, str) or not _VIDEO_TOOL_SAFE_NAME.fullmatch(name):
                    name = "other"
                state = step.get("state")
                state = state if state in ("ACTIVE", "DONE") else "other"
                params = info.get("parameters")
                # Matching is only a target-routing observation, not media proof.
                match = isinstance(params, (dict, list)) and video_id in json.dumps(
                    params, ensure_ascii=False
                )
                has_error = bool(info.get("error"))
                is_web_tool = any(k in name.lower() for k in _VIDEO_ACCESS_TOOLS)
                success = (
                    state == "DONE" and is_web_tool and match and not has_error
                )
                result["tool_steps"].append({
                    "name": name, "state": state,
                    "target_match": match, "error": has_error,
                    "web_tool": is_web_tool,
                })
                if state == "DONE":
                    result["tool_done_count"] += 1
                if match:
                    result["tool_target_match_count"] += 1
                if has_error:
                    result["tool_error_count"] += 1
                    if _PERMISSION_DIAGNOSTIC.search(
                        json.dumps(info["error"], ensure_ascii=False)
                    ):
                        result["permission_hint"] = True
                if success:
                    result["successful_target_web_steps"] += 1
        result["tool_steps"] = result["tool_steps"][:24]
        if result["successful_target_web_steps"]:
            result["access_level"] = "web_tool_observed_not_media_proof"
        if result["agent_selection"] == "different":
            result["next_action"] = "AGY_SELECTED_WRONG_AGENT"
        elif result["permission_hint"]:
            result["next_action"] = "CHECK_SCOPED_READ_URL_PERMISSIONS"
        elif not result["init_seen"]:
            result["next_action"] = "CHECK_AGY_STREAM_FORMAT"
        elif not result["available_web_tools"]:
            result["next_action"] = "CHECK_AGENT_WEB_TOOL_AVAILABILITY"
        elif result["tool_done_count"] == 0:
            result["next_action"] = "NO_COMPLETED_TOOLS_CHECK_AGENT_REASONING"
        elif result["tool_target_match_count"] == 0:
            result["next_action"] = "TOOLS_DID_NOT_TARGET_REQUESTED_VIDEO"
        elif not result["successful_target_web_steps"]:
            result["next_action"] = "WEB_TOOL_NOT_SUCCESSFULLY_OBSERVED"
        else:
            result["next_action"] = "WEB_PAGE_OBSERVED_MEDIA_NOT_ESTABLISHED"
        return result
    return {"found": False, "reason": "NO_READABLE_ARCHIVED_VIDEO_ATTEMPT"}
