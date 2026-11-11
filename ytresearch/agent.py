"""Minimal supported Antigravity CLI adapter (official agy, not an OAuth proxy)."""
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import time

from .storage import read_json
from .progress import report
from .diagnostics import archive_agy_attempt, find_matching_agy_archive
from .normalization import normalize_category_claims, ENGLISH_CATEGORY_ALIASES, anchored_unknown_empty_slots


def parse_json_documents(response: str):
    """Parse a strict sequence of complete JSON objects, without dropping any text.

    Some agy 1.5.0 headless sessions concatenate several JSON replies into one
    response. Never use a regex to extract only the first valid-looking object.
    """
    decoder = json.JSONDecoder()
    position, documents = 0, []
    while position < len(response):
        while position < len(response) and response[position].isspace():
            position += 1
        if position == len(response):
            break
        if len(documents) >= 32:
            raise RuntimeError("AGY_TOO_MANY_JSON_DOCUMENTS: 응답 객체가 32개를 초과합니다")
        try:
            value, next_position = decoder.raw_decode(response, position)
        except json.JSONDecodeError as exc:
            tail = response[position:].lstrip()
            if tail.startswith("```"):
                kind = "code_fence"
            elif tail.startswith(("{", "[")):
                kind = "json_start"
            elif tail[:1].isalpha():
                kind = "text_start"
            elif not tail:
                kind = "empty"
            else:
                kind = "other"
            raise RuntimeError(
                f"AGY_UNSTRUCTURED_RESPONSE: 구조화 응답에 완전한 JSON이 아닌 구간이 있습니다 "
                f"(line={exc.lineno}, col={exc.colno}, response_chars={len(response)}, "
                f"documents_decoded={len(documents)}, tail_kind={kind}, "
                f"tail_chars={len(tail)}, "
                f"decoded_shapes={describe_document_shapes(documents) if documents else 'none'})"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError("AGY_INVALID_RESPONSE_TYPE: JSON 객체만 허용합니다")
        documents.append(value)
        position = next_position
    if not documents:
        raise RuntimeError("AGY_EMPTY_STRUCTURED_RESULT: JSON 객체가 없습니다")
    return documents


def merge_json_documents(documents):
    """Preserve all valid repeated lists, deduplicate exact repeats only.

    Conflicting section rewrites fail closed rather than silently selecting one.
    """
    if len(documents) == 1:
        return documents[0]
    if all(item == documents[0] for item in documents):
        return documents[0]
    keys = set(documents[0])
    if any(set(item) != keys for item in documents):
        raise RuntimeError("AGY_CONFLICTING_DOCUMENTS: JSON 객체들의 필드가 서로 다릅니다")
    list_key = next((key for key in ("claims", "sections", "issues") if keys == {key}), None)
    if list_key is None or any(not isinstance(item[list_key], list) for item in documents):
        raise RuntimeError("AGY_CONFLICTING_DOCUMENTS: 병합 가능한 목록 스키마가 아닙니다")
    if list_key == "sections":
        merged_by_number = {}
        for item in documents:
            for section in item[list_key]:
                if not isinstance(section, dict) or not isinstance(section.get("number"), int):
                    raise RuntimeError("AGY_CONFLICTING_DOCUMENTS: 섹션 형식이 유효하지 않습니다")
                number = section["number"]
                if number in merged_by_number and merged_by_number[number] != section:
                    raise RuntimeError(f"AGY_CONFLICTING_DOCUMENTS: 섹션 {number} 내용이 서로 다릅니다")
                merged_by_number[number] = section
        return {"sections": [merged_by_number[n] for n in sorted(merged_by_number)]}
    output, seen = [], set()
    for item in documents:
        for value in item[list_key]:
            marker = json.dumps(value, sort_keys=True, ensure_ascii=False)
            if marker not in seen:
                seen.add(marker)
                output.append(value)
    return {list_key: output}


def require_schema_shape(value: dict, schema: dict | None):
    """Validate root output shape before caching or trusting an AI response.

    Error details contain trusted schema field names and value types only.
    Never echo untrusted model keys, claim text, transcript, or OAuth URLs.
    """
    if schema is None:
        return
    properties = schema.get("properties", {})
    required = schema.get("required", ())
    if not isinstance(value, dict):
        raise RuntimeError("AGY_SCHEMA_MISMATCH: 최상위 결과가 JSON 객체가 아닙니다")
    missing = [field for field in required if field not in value]
    wrong_types = []
    for field, spec in properties.items():
        if field not in value:
            continue
        typ = spec.get("type")
        item = value[field]
        if typ == "array" and not isinstance(item, list):
            wrong_types.append(f"{field}: expected=array, got={type(item).__name__}")
        elif typ == "object" and not isinstance(item, dict):
            wrong_types.append(f"{field}: expected=object, got={type(item).__name__}")
        elif typ == "string" and not isinstance(item, str):
            wrong_types.append(f"{field}: expected=string, got={type(item).__name__}")
    extras = len(set(value) - set(properties)) if schema.get("additionalProperties") is False else 0
    if missing or wrong_types or extras:
        raise RuntimeError(
            "AGY_SCHEMA_MISMATCH: 모델 응답 형식 불일치 "
            f"(missing={missing}, wrong_types={wrong_types}, "
            f"unexpected_fields_count={extras}, root_field_count={len(value)}). "
            "요청한 JSON Schema에 맞는 claims/sections/issues 배열이 필요합니다."
        )


def describe_document_shapes(documents, *, max_documents=8):
    """Show field names and types, never any field *values* or source content.

    Unknown keys that do not look like standard identifiers are redacted.
    These summaries can be pasted into a support conversation safely.
    """
    summaries = []
    for index, document in enumerate(documents[:max_documents], 1):
        fields = []
        for key, value in list(document.items())[:12]:
            safe_key = key if isinstance(key, str) and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]{0,31}", key
            ) else "<redacted-key>"
            if isinstance(value, list):
                kind = f"list({len(value)})"
            elif isinstance(value, dict):
                kind = f"object({len(value)})"
            elif isinstance(value, str):
                kind = f"string({len(value)})"
            else:
                kind = type(value).__name__
            fields.append(f"{safe_key}:{kind}")
        if len(document) > 12:
            fields.append(f"+{len(document)-12} fields")
        summaries.append(f"{index}=[{', '.join(fields)}]")
    if len(documents) > max_documents:
        summaries.append(f"+{len(documents)-max_documents} documents")
    return "; ".join(summaries)


def parse_agy_envelope(stdout: str):
    """Read classic JSON or streaming NDJSON and use only terminal result.

    In stream-json, progress updates may contain partial model text, so these
    must never be passed as the final claims response. The final result event
    is a documented terminal boundary.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict) and "event" not in envelope:
        return envelope
    if isinstance(envelope, dict) and envelope.get("event") == "result":
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        raise RuntimeError("AGY_STREAM_INVALID_RESULT: 최종 result 객체가 없습니다")
    events = [line for line in stdout.splitlines() if line.strip()]
    if not events:
        raise RuntimeError("AGY_EMPTY_STDOUT: CLI 출력이 비어 있습니다")
    if len(events) > 10000:
        raise RuntimeError("AGY_STREAM_TOO_MANY_EVENTS: 이벤트 수 제한 초과")
    final = None
    completed_responses = []
    seen_result = False
    for index, line in enumerate(events, 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"AGY_INVALID_ENVELOPE_JSON: stream line={index}, col={exc.colno}, "
                f"total_lines={len(events)}"
            ) from exc
        if not isinstance(event, dict) or event.get("event") not in (
            "init", "step_update", "result"
        ):
            raise RuntimeError(
                f"AGY_STREAM_INVALID_EVENT: line={index}, event_kind_not_supported"
            )
        if seen_result:
            raise RuntimeError("AGY_STREAM_TRAILING_EVENT: 최종 result 이후 추가 이벤트 존재")
        if event["event"] == "step_update":
            update = event.get("step_update")
            if isinstance(update, dict) and (
                update.get("step_type") == "agent_response"
                and update.get("state") == "DONE"
            ):
                delta = update.get("text_delta")
                if isinstance(delta, str) and delta.strip():
                    completed_responses.append(delta)
        if event["event"] == "result":
            final = event.get("result")
            seen_result = True
    if not isinstance(final, dict):
        raise RuntimeError(
            f"AGY_STREAM_MISSING_RESULT: final result 없음, events={len(events)}"
        )
    # Private parser metadata, not a field emitted by the CLI.
    final = dict(final)
    final["_completed_agent_responses"] = completed_responses[-8:]
    return final


def try_last_completed_response(envelope: dict, original_response: str, schema: dict | None):
    """Use the final documented DONE agent message only when fully schema-valid.

    Never use an earlier step or silently strip terminal text.
    """
    candidates = envelope.get("_completed_agent_responses") or []
    if not candidates:
        return None
    candidate = candidates[-1]
    if not isinstance(candidate, str) or not candidate.strip() or candidate == original_response:
        return None
    try:
        documents = parse_json_documents(candidate)
        documents = [normalize_category_claims(doc, schema)[0] for doc in documents]
        for document in documents:
            require_schema_shape(document, schema)
        recovered = merge_json_documents(documents)
        require_schema_shape(recovered, schema)
    except RuntimeError:
        return None
    usage = dict(envelope.get("usage") or {})
    usage["response_source"] = "last_completed_agent_response"
    usage["response_json_documents"] = len(documents)
    usage["completed_agent_responses"] = len(candidates)
    report(
        "[AGY] 최종 통합 응답 대신 마지막 완료 모델 응답의 JSON을 사용했습니다. "
        "그 외 모델 메시지는 병합하지 않았으므로 추출 결과의 근거를 검수하세요.",
        error=True,
    )
    return recovered, usage


def quarantine_mixed_extraction(documents, schema):
    """Opt-in recovery of observed four-document AGY output without discarding raw data.

    Only the first document is accepted as evidence-backed claims. Other
    documents remain quarantined and traceable in the unmodified raw archive.
    Strictly reject other shapes and string-encoded JSON data.
    """
    if (not schema or set(schema.get("properties", {})) != {"claims"}
            or len(documents) != 4):
        return None
    first, status_doc, strings_a, strings_b = documents
    if (not isinstance(first, dict) or not isinstance(status_doc, dict)
            or set(status_doc) != {"status", "message"}
            or not isinstance(status_doc.get("status"), str)
            or status_doc["status"].strip().lower() not in {"completed", "succeeded", "success"}
            or not isinstance(status_doc.get("message"), str)
            or not isinstance(strings_a, dict) or strings_a != strings_b
            or len(first) != 18 or set(first) != set(strings_a)
            or not all(isinstance(value, str) for value in strings_a.values())):
        return None
    # Do not silently dismiss serialized JSON which might hold extra claims.
    for value in strings_a.values():
        try:
            json.loads(value)
        except ValueError:
            pass
        else:
            return None
    try:
        normalized, changed = normalize_category_claims(first, schema)
        require_schema_shape(normalized, schema)
        if not isinstance(normalized.get("claims"), list) or not normalized["claims"]:
            return None
    except RuntimeError:
        return None
    unique_hashes = [
        hashlib.sha256(
            json.dumps(documents[index], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for index in (1, 2, 3)
    ]
    return normalized, {
        "quarantined_document_indices": [2, 3, 4],
        "quarantined_document_sha256": unique_hashes,
        "quarantine_reason": "mixed_non_structured_agy_documents",
        "review_required": True,
        "recovered_document_index": 1,
        "response_json_documents": 4,
        "unrecognized_empty_category_slots": list(anchored_unknown_empty_slots(first)),
        "normalized_from": "english_category_keys" if changed else "claims",
        "kind_default": "source_claim",
    }


def parse_structured_response(stdout: str, stderr: str, schema: dict | None = None, *, quarantine_mixed: bool = False):
    """Validate the CLI envelope and fail explicitly on silent empty SUCCESS runs.

    Never include the raw stdout/stderr in error messages: the CLI can emit
    short-lived account-verification links or content from private prompts.
    """
    if not stdout.strip():
        raise RuntimeError("AGY_EMPTY_STDOUT: CLI 응답이 비어 있습니다 (stderr_present=%s)" % bool(stderr.strip()))
    envelope = parse_agy_envelope(stdout)
    if not isinstance(envelope, dict):
        raise RuntimeError("AGY_INVALID_ENVELOPE_TYPE: JSON 객체가 아닙니다")

    status = envelope.get("status")
    if status != "SUCCESS":
        raise RuntimeError(
            f"AGY_NON_SUCCESS_STATUS: status={status!r} "
            f"(check CLI stderr and account eligibility locally)"
        )

    data = envelope.get("structured_output")
    if isinstance(data, dict):
        unknown_empty_slots = anchored_unknown_empty_slots(data)
        normalized, changed = normalize_category_claims(data, schema)
        require_schema_shape(normalized, schema)
        usage = dict(envelope.get("usage") or {})
        if changed:
            usage["normalized_from"] = (
                "category_fields" if "claims" in data else
                "english_category_keys" if any(key in ENGLISH_CATEGORY_ALIASES for key in data)
                else "category_headings"
            )
            usage["kind_default"] = "source_claim"
        if unknown_empty_slots:
            usage["unrecognized_empty_category_slots"] = list(unknown_empty_slots)
            report(
                "[AGY] 모든 다른 카테고리 위치가 검증된 18개 분류 중 "
                f"빈 배열로 반환된 미등록 카테고리 슬롯 {list(unknown_empty_slots)}을 "
                "내용 생성 없이 통과했습니다.",
                error=True,
            )
        return normalized, usage
    if data is not None:
        raise RuntimeError(
            "AGY_INVALID_STRUCTURED_OUTPUT: structured_output이 JSON 객체가 아닙니다"
        )

    response = envelope.get("response")
    if not isinstance(response, str) or not response.strip():
        stderr_lower = stderr.lower()
        permission_denied = any(
            term in stderr_lower for term in ("auto-denied", "permission", "no output produced")
        )
        usage = envelope.get("usage") or {}
        raise RuntimeError(
            "AGY_EMPTY_STRUCTURED_RESULT: CLI가 SUCCESS를 반환했지만 "
            "structured_output이 없고 response도 비어 있습니다 "
            f"(turns={envelope.get('num_turns')}, "
            f"output_tokens={usage.get('output_tokens')}, "
            f"permission_hint={permission_denied}, stderr_present={bool(stderr.strip())}). "
            "--mode plan 또는 headless 권한 거부 여부를 별도 점검하세요."
        )
    try:
        documents = parse_json_documents(response)
    except RuntimeError as primary_error:
        # The official stream has a terminal result AND completed agent_response
        # step events. A composite terminal response can contain commentary after
        # one complete JSON object. Use only the LAST completed agent response if
        # it is independently valid; never strip unknown trailing content.
        recovered = try_last_completed_response(envelope, response, schema)
        if recovered is not None:
            return recovered
        candidates = envelope.get("_completed_agent_responses") or []
        # Preserve failure details; never return the first partial JSON.
        raise RuntimeError(
            f"{primary_error} "
            f"(completed_agent_responses={len(candidates)}, "
            f"last_completed_chars={len(candidates[-1]) if candidates else 0})"
        ) from primary_error
    if quarantine_mixed:
        candidate = quarantine_mixed_extraction(documents, schema)
        if candidate is not None:
            normalized, metadata = candidate
            usage = dict(envelope.get("usage") or {})
            usage.update(metadata)
            report(
                "[AGY] 주의: 첫 번째 분석 문서만 조건부 정규화. "
                "2~4번 JSON 문서는 원본 보관·해시 기록 후 검수 대기 처리했습니다. "
                "이 청크의 결과를 최종 검증 완료로 간주하지 마세요.",
                error=True,
            )
            return normalized, usage
    unknown_empty_slots = sorted({
        slot for doc in documents
        for slot in anchored_unknown_empty_slots(doc)
    })
    had_category_fields = any(
        isinstance(doc, dict) and isinstance(doc.get("claims"), list)
        and any(isinstance(entry, dict) and "category_id" in entry
                for entry in doc["claims"])
        for doc in documents
    )
    had_english_category_keys = any(
        isinstance(doc, dict)
        and any(key in ENGLISH_CATEGORY_ALIASES for key in doc)
        for doc in documents
    )
    converted = [normalize_category_claims(doc, schema) for doc in documents]
    normalized_any = any(changed for _, changed in converted)
    documents = [item for item, _ in converted]
    mismatches = []
    for idx, document in enumerate(documents, 1):
        try:
            require_schema_shape(document, schema)
        except RuntimeError as exc:
            mismatches.append((idx, str(exc)))
    if mismatches:
        recovered = try_last_completed_response(envelope, response, schema)
        if recovered is not None:
            return recovered
        first_index, first_error = mismatches[0]
        raise RuntimeError(
            f"{first_error} "
            f"(document={first_index}/{len(documents)}, "
            f"invalid_documents={[idx for idx, _ in mismatches]}, "
            f"turns={envelope.get('num_turns')}, response_chars={len(response)}). "
            "AGY_RESPONSE_SHAPES: " + describe_document_shapes(documents)
        )
    parsed = merge_json_documents(documents)
    require_schema_shape(parsed, schema)
    usage = dict(envelope.get("usage") or {})
    if len(documents) > 1:
        usage["response_json_documents"] = len(documents)
    if normalized_any:
        usage["normalized_from"] = (
            "category_fields" if had_category_fields else
            "english_category_keys" if had_english_category_keys else
            "category_headings"
        )
        usage["kind_default"] = "source_claim"
    if unknown_empty_slots:
        usage["unrecognized_empty_category_slots"] = unknown_empty_slots
        report(
            "[AGY] 18개 분류 중 모든 다른 위치가 검증된 빈 배열 카테고리 "
            f"슬롯 {unknown_empty_slots}은 근거 항목이 없어 통과했습니다.",
            error=True,
        )
    return parsed, usage


class Agy:
    def __init__(self, root: Path):
        self.root = root
        self.config = read_json(root / "config" / "agents.json")
        self.binary = shutil.which("agy")
        if not self.binary:
            raise RuntimeError("Antigravity CLI 'agy'를 PATH에서 찾지 못했습니다. 공식 CLI를 설치하고 agy 로그인하십시오.")

    def _invoke(self, command, role: str, attempt: int, attempts: int):
        """Capture a complete CLI envelope while reporting time-based heartbeats."""
        heartbeat = max(1.0, float(self.config.get("heartbeat_seconds", 10)))
        timeout_seconds = max(1.0, float(self.config.get("cli_timeout_seconds", 680)))
        started = time.monotonic()
        with subprocess.Popen(
            command, cwd=str(self.root), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace",
        ) as proc:
            while True:
                elapsed = time.monotonic() - started
                remaining = timeout_seconds - elapsed
                if remaining <= 0:
                    proc.kill()
                    proc.communicate()
                    raise RuntimeError(f"AGY_TIMEOUT: {role} {timeout_seconds:.0f}초 초과")
                try:
                    stdout, stderr = proc.communicate(timeout=min(heartbeat, remaining))
                    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    report(
                        f"[AGY] {role}: 모델 처리 중 · 시도 {attempt}/{attempts} · "
                        f"{elapsed:.0f}초 경과 (모델 내부 진행률은 제공되지 않음)",
                        error=True,
                    )
                except KeyboardInterrupt:
                    proc.kill()
                    proc.communicate()
                    raise

    def run(self, role: str, prompt: str, schema: dict, *, context: dict | None = None,
            archives_only: bool = False, quarantine_mixed: bool = False):
        settings = self.config.get(role)
        if not settings:
            raise ValueError(f"존재하지 않는 역할: {role}")
        model = settings["model"]
        if not model or model == "SET_FROM_AGY_MODELS":
            raise RuntimeError(f"config/agents.json의 {role}.model에 'agy models'에 표시되는 실제 모델명을 지정하십시오.")
        if len(prompt) > 27000:
            raise RuntimeError("Windows 명령행 길이 안전 한도를 초과했습니다. 청크 크기를 줄이십시오.")
        schema_path = self.root / "data" / "schema_cache" / f"{role}.json"
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        # Use a tool-free custom agent instead of plan mode. In plan mode the
        # CLI prepends /plan and may attempt tools, which headless cannot approve.
        # The definition lives in this repo so model-only research is reproducible.
        agent_name = self.config.get("agy_agent", "yt-research-no-tools")
        agent_file = self.root / ".agents" / "agents" / f"{agent_name}.md"
        if not agent_file.is_file():
            raise RuntimeError(
                f"AGY_AGENT_MISSING: {agent_file}. "
                "프로젝트 전용 도구 없는 에이전트 정의 파일이 필요합니다."
            )
        output_format = self.config.get("agy_output_format", "stream-json")
        if output_format not in ("json", "stream-json"):
            raise ValueError("agy_output_format은 json 또는 stream-json이어야 합니다")
        if quarantine_mixed and not archives_only:
            raise RuntimeError(
                "AGY_QUARANTINE_OFFLINE_ONLY: 혼합 응답의 검수 대기 복구는 "
                "저장된 원본 아카이브로만 허용합니다."
            )
        if archives_only:
            saved = find_matching_agy_archive(
                self.root, role=role, model=model, prompt=prompt, context=context or {}
            )
            if saved is None:
                raise RuntimeError(
                    "AGY_ARCHIVE_NOT_FOUND: 동일한 영상·청크·모델·프롬프트의 "
                    "기존 응답이 없습니다. 네트워크 모델 호출은 수행하지 않았습니다."
                )
            folder, archived_stdout, archived_stderr = saved
            extracted, usage = parse_structured_response(
                archived_stdout, archived_stderr, schema=schema,
                quarantine_mixed=quarantine_mixed,
            )
            usage = dict(usage or {})
            usage["replayed_from_archive"] = True
            usage["archive_folder"] = folder.name
            report(f"[AGY] 기존 저장 원본 복구: {folder}", error=True)
            return extracted, usage
        command = [
            self.binary, "-p", prompt,
            "--model", model,
            "--effort", settings["effort"],
            "--agent", agent_name,
            "--output-format", output_format,
            "--json-schema", str(schema_path),
            "--print-timeout", self.config.get("print_timeout", "10m"),
            "--disable-slash-commands",
        ]
        errors = []
        attempts = int(self.config.get("max_retries", 2)) + 1

        def preserve(result, attempt_number, started, failure):
            # Save BOTH parsed and failed CLI responses. A later pipeline
            # validation may fail after adapter parsing, so only saving
            # adapter failures would lose the result the user needs.
            if not self.config.get("archive_agy_responses", True):
                return
            try:
                folder = archive_agy_attempt(
                    self.root, role=role, model=model, attempt=attempt_number,
                    context=context, stdout=result.stdout if result else "",
                    stderr=result.stderr if result else "",
                    exit_code=result.returncode if result else None,
                    error=failure, duration_seconds=time.monotonic() - started,
                    prompt=prompt,
                )
                report(f"[AGY] 응답 원본 로컬 보관: {folder}", error=True)
            except (OSError, ValueError, TypeError) as archive_error:
                report(
                    f"[AGY] DIAGNOSTIC_ARCHIVE_FAILURE: {type(archive_error).__name__}",
                    error=True,
                )

        for attempt in range(1, attempts + 1):
            report(
                f"[AGY] {role}: {model} 요청 시작 (시도 {attempt}/{attempts})",
                error=True,
            )
            result = None
            started = time.monotonic()
            try:
                result = self._invoke(command, role, attempt, attempts)
                if result.returncode:
                    raise RuntimeError(
                        f"AGY_EXIT_NONZERO: exit={result.returncode}, "
                        f"stderr_present={bool(result.stderr.strip())}. "
                        "CLI stderr를 로컬에서 직접 확인하세요."
                    )
                parsed = parse_structured_response(result.stdout, result.stderr, schema=schema)
            except (OSError, ValueError, RuntimeError) as exc:
                preserve(result, attempt, started, exc)
                errors.append(str(exc))
                report(
                    f"[AGY] {role}: 시도 {attempt}/{attempts} 실패 · {str(exc)[:240]}",
                    error=True,
                )
                if attempt == attempts:
                    break
                time.sleep(min(2 ** (attempt - 1), 5))
            else:
                preserve(result, attempt, started, None)
                return parsed
        raise RuntimeError("모델 호출 실패: " + " | ".join(errors[-2:]))
