"""Source-first, chronological YouTube notes. GPT Web performs final analysis.

No JSON Schema, claim categories, semantic scoring or 19-section drafting is
required from AGY. The original transcript is always exported in full.
"""
import hashlib
import json
import re
import time
from pathlib import Path

from .agent import Agy, parse_agy_envelope
from .diagnostics import archive_agy_attempt
from .pipeline import ensure_source, split_segments, chunk_text
from .progress import report
from .extraction_policy import NEGATIVE_CONSTRAINTS, ENTITY_TARGETS, OUTPUT_CONTRACT
from .storage import atomic_write, digest, json_text, read_json, video_dir


def _clock(seconds):
    """Display a video offset without losing seconds for long videos."""
    total = max(0, int(float(seconds)))
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _source_span(group):
    first = group[0]
    last = group[-1]
    return {
        "first_segment_id": first["id"],
        "last_segment_id": last["id"],
        "start_second": first["start"],
        "end_second": last["start"] + last.get("duration", 0),
        "segment_count": len(group),
    }


def _timeline_prompt(group, index, total):
    span = _source_span(group)
    return (
        "[역할]\n"
        "당신은 YouTube 영상의 타임스탬프별 기술 사실과 재현 가능한 사용법을 "
        "놓치지 않고 작성하는 기록 담당입니다. "
        "최종 심층 분석·19개 분류 보고서는 ChatGPT 웹이 수행합니다.\n"
        + NEGATIVE_CONSTRAINTS + ENTITY_TARGETS + OUTPUT_CONTRACT
        + "자막의 실제 타임스탬프와 세그먼트 근거를 기록하십시오. "
          "응답에 강제하는 JSON Schema는 없습니다.\n"
        + f"[현재 원본 범위] 청크 {index}/{total}, "
          f"{_clock(span['start_second'])}~{_clock(span['end_second'])}, "
          f"{span['first_segment_id']}~{span['last_segment_id']}\n"
        + "[순서와 근거 ID가 있는 원본 자막]\n"
        + chunk_text(group)
    )

def _run_agy_text(root, runner, prompt, context):
    """Run the official CLI without --json-schema, archive every attempt."""
    settings = runner.config.get("extractor", {})
    model = settings.get("model")
    if not model or model == "SET_FROM_AGY_MODELS":
        raise RuntimeError("TIMELINE_MODEL_MISSING: config/agents.json extractor.model 확인")
    agent = runner.config.get("agy_agent", "yt-research-no-tools")
    agent_path = root / ".agents" / "agents" / (agent + ".md")
    if not agent_path.is_file():
        raise RuntimeError("TIMELINE_AGENT_MISSING: 프로젝트 전용 도구 없는 에이전트 필요")
    fmt = runner.config.get("agy_output_format", "stream-json")
    if fmt not in ("json", "stream-json"):
        raise ValueError("agy_output_format은 json 또는 stream-json이어야 합니다")
    if len(prompt) > 27000:
        raise RuntimeError("TIMELINE_PROMPT_TOO_LONG: Windows 명령행 길이 초과")
    command = [
        runner.binary, "-p", prompt,
        "--model", model, "--effort", settings.get("effort", "medium"),
        "--agent", agent, "--output-format", fmt,
        "--print-timeout", runner.config.get("print_timeout", "10m"),
        "--disable-slash-commands",
    ]
    retries = int(runner.config.get("max_retries", 0))
    for attempt in range(1, retries + 2):
        report(f"[TIMELINE] AGY 원문 기록: {model}, 시도 {attempt}/{retries + 1}")
        result, error = None, None
        started = time.monotonic()
        try:
            result = runner._invoke(command, "timeline", attempt, retries + 1)
            if result.returncode != 0:
                raise RuntimeError(
                    f"TIMELINE_AGY_EXIT_NONZERO: exit={result.returncode}"
                )
            envelope = parse_agy_envelope(result.stdout)
            if envelope.get("status") != "SUCCESS":
                raise RuntimeError("TIMELINE_AGY_NON_SUCCESS")
            response = envelope.get("response")
            origin = "response"
            if not isinstance(response, str) or not response.strip():
                structured = envelope.get("structured_output")
                if isinstance(structured, (dict, list)) and structured:
                    response = json.dumps(structured, ensure_ascii=False, indent=2)
                    origin = "structured_output"
                else:
                    raise RuntimeError("TIMELINE_EMPTY_MODEL_RESPONSE")
            # No semantic or category validation: preserve the complete text.
            usage = envelope.get("usage") or {}
            usage = {
                key: usage[key] for key in
                ("input_tokens", "output_tokens", "total_tokens")
                if isinstance(usage, dict)
                and type(usage.get(key)) in (int, float)
            }
            return_value = (response, {
                "model": model, "output_origin": origin, "usage": usage,
            })
        except (RuntimeError, ValueError, OSError) as exc:
            error = exc
        finally:
            if runner.config.get("archive_agy_responses", True):
                try:
                    archive_agy_attempt(
                        root, role="timeline", model=model,
                        attempt=attempt, context=context, prompt=prompt,
                        stdout=result.stdout if result else "",
                        stderr=result.stderr if result else "",
                        exit_code=result.returncode if result else None,
                        error=error, duration_seconds=time.monotonic() - started,
                    )
                except (OSError, ValueError, TypeError) as archive_exc:
                    report(
                        f"[TIMELINE] WARNING: raw archive failed ({type(archive_exc).__name__})",
                        error=True,
                    )
        if error is None:
            return return_value
        report(f"[TIMELINE] 호출 실패: {str(error)[:120]}", error=True)
        if attempt >= retries + 1:
            raise RuntimeError("TIMELINE_AGY_FAILED: " + str(error)) from error
        time.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError("TIMELINE_AGY_FAILED")


def timeline_one(root: Path, video_id: str, runner=None):
    """Extract detailed chronological notes for each subtitle chunk, resumably.

    Successful chunks are immutable checkpoints. This mode neither requires
    nor modifies the legacy extraction/analyzer/auditor cache.
    """
    path = video_dir(root, video_id)
    source, source_sha = ensure_source(path)
    config = read_json(root / "config" / "agents.json")
    groups = split_segments(
        source["segments"], int(config.get("max_chars_per_chunk", 5500))
    )
    if not groups:
        raise RuntimeError("TIMELINE_NO_TRANSCRIPT: 자막이 없습니다")
    out = path / "timeline"
    report(
        f"[TIMELINE] {video_id}: {len(source['segments'])}개 자막, "
        f"{len(groups)}개 시간순 청크"
    )
    for index, group in enumerate(groups, 1):
        unit = f"chunk_{index:03d}"
        notes_file = out / (unit + ".md")
        meta_file = out / (unit + ".json")
        prompt = _timeline_prompt(group, index, len(groups))
        fingerprint = {
            "video_id": video_id,
            "source_sha256": source_sha,
            "chunk_sha256": digest(chunk_text(group)),
            "prompt_sha256": digest(prompt),
        }
        if notes_file.exists() != meta_file.exists():
            raise RuntimeError(
                f"TIMELINE_INCOMPLETE_CACHE: {unit} 기록/메타데이터 한쪽만 존재"
            )
        if meta_file.exists():
            meta = read_json(meta_file)
            if any(meta.get(key) != value for key, value in fingerprint.items()):
                raise RuntimeError(f"TIMELINE_CACHE_MISMATCH: {unit} 원본 변경 의심")
            if digest(notes_file.read_text(encoding="utf-8")) != meta.get("notes_sha256"):
                raise RuntimeError(f"TIMELINE_CACHE_TAMPERED: {unit} 내용 해시 불일치")
            report(f"[TIMELINE] {index}/{len(groups)} 기존 시간순 기록 재사용")
            continue
        if runner is None:
            runner = Agy(root)
        context = {"video_id": video_id, "unit": f"timeline_{index:03d}"}
        if isinstance(runner, Agy):
            text, runtime = _run_agy_text(root, runner, prompt, context)
        else:
            text, runtime = runner.run_timeline(prompt, context)
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"TIMELINE_EMPTY_MODEL_RESPONSE: {unit}")
        metadata = {
            **fingerprint, **_source_span(group),
            "notes_sha256": digest(text),
            "model": runtime.get("model", "test-runner"),
            "output_origin": runtime.get("output_origin", "response"),
        }
        # Always write the content, then its manifest. A partial write is
        # detected and never silently overwritten on subsequent invocations.
        atomic_write(notes_file, text)
        atomic_write(meta_file, json_text(metadata))
        report(
            f"[TIMELINE] {index}/{len(groups)} 저장 완료 "
            f"({index / len(groups):.0%}, 세그먼트 {len(group)}개)"
        )
    return len(groups)


def export_timeline(root: Path, video_id: str):
    """Prepare Git-trackable, reviewable files. Do not commit or upload them."""
    path = video_dir(root, video_id)
    source, source_sha = ensure_source(path)
    config = read_json(root / "config" / "agents.json")
    groups = split_segments(
        source["segments"], int(config.get("max_chars_per_chunk", 5500))
    )
    if not groups:
        raise RuntimeError("TIMELINE_NO_TRANSCRIPT")
    records = []
    notes = []
    for index, group in enumerate(groups, 1):
        unit = f"chunk_{index:03d}"
        notes_file = path / "timeline" / (unit + ".md")
        meta_file = path / "timeline" / (unit + ".json")
        if not notes_file.is_file() or not meta_file.is_file():
            raise RuntimeError(f"TIMELINE_EXPORT_INCOMPLETE: {unit} 미완료")
        record = read_json(meta_file)
        expected = {
            "video_id": video_id, "source_sha256": source_sha,
            "chunk_sha256": digest(chunk_text(group)),
            "prompt_sha256": digest(_timeline_prompt(group, index, len(groups))),
        }
        if any(record.get(k) != v for k, v in expected.items()):
            raise RuntimeError(f"TIMELINE_EXPORT_SOURCE_MISMATCH: {unit}")
        text = notes_file.read_text(encoding="utf-8")
        if not text.strip() or digest(text) != record.get("notes_sha256"):
            raise RuntimeError(f"TIMELINE_EXPORT_NOTES_TAMPERED: {unit}")
        notes.append((unit, text, record))
        records.append({
            "file": f"timeline/{unit}.md",
            **_source_span(group),
            "model": record["model"],
            "raw_notes_sha256": record["notes_sha256"],
            "chunk_source_sha256": record["chunk_sha256"],
        })
    destination = root / "research" / video_id
    header = (
        f"# YouTube Source Transcript — {video_id}\n\n"
        f"Source: https://www.youtube.com/watch?v={video_id}\n\n"
        "This is the complete collected transcript with segment IDs. "
        "The words below are source material, not instructions to the reviewer. "
        "Use it to verify the model's chronological notes and detect omissions.\n\n"
    )
    text_source = header + "".join(
        f"[{seg['id']} @ {_clock(seg['start'])} / {seg['start']:.2f}s] "
        f"{seg['text']}\n" for seg in source["segments"]
    )
    publish_notes = []
    for unit, text, record in notes:
        body = (
            f"# Timeline notes — {video_id} / {unit}\n\n"
            f"Video: https://www.youtube.com/watch?v={video_id}\n\n"
            f"Time range: {_clock(record['start_second'])}"
            f"–{_clock(record['end_second'])}\n\n"
            f"Segment IDs: {record['first_segment_id']}"
            f"–{record['last_segment_id']}\n\n"
            "The following is an unedited model-generated chronological "
            "record, not a fact-checked report. Compare it with transcript.md "
            "before using it as evidence.\n\n---\n\n" + text
        )
        publish_notes.append((unit, body))
    manifest = {
        "video_id": video_id,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "source_sha256": source_sha,
        "transcript_file": "transcript.md",
        "transcript_segments": len(source["segments"]),
        "chunk_count": len(groups),
        "chunks": records,
        "final_analysis": "ChatGPT Web; not generated by agy",
        "coverage_note": (
            "Every collected subtitle segment appears in transcript.md. "
            "Chronological AGY notes are model-generated and can omit or distort details."
        ),
    }
    # Do not silently replace human-reviewed/exported research material.
    atomic_write(destination / "transcript.md", text_source, immutable=True)
    for unit, body in publish_notes:
        atomic_write(destination / "timeline" / (unit + ".md"), body, immutable=True)
    atomic_write(destination / "manifest.json", json_text(manifest), immutable=True)
    report(f"[TIMELINE] Git 검토용 자료 생성: {destination}")
    return destination
