"""Optional media-backed video research, independent of subtitle collection.

AGY headless has no documented native YouTube-video input flag: tool access
is tracked separately and never treated as proof of full audiovisual coverage.
Gemini Interactions explicitly receives a video URI when chosen.
All generated text remains unverified research evidence for ChatGPT Web.
"""
import json
import os
import re
from pathlib import Path

from .agent import Agy, parse_agy_envelope
from .diagnostics import archive_agy_attempt, inspect_video_access_archive
from .extraction_policy import NEGATIVE_CONSTRAINTS, ENTITY_TARGETS, OUTPUT_CONTRACT
from .pipeline import ensure_source, split_segments, chunk_text
from .progress import report
from .storage import atomic_write, digest, json_text, read_json, video_dir
from .collector import VIDEO_RE

VIDEO_REF = "https://www.youtube.com/watch?v={}"
VIDEO_EVIDENCE = "video_evidence"
ACCESS_STATES = {
    "agy": "web_access_observed_not_media_proof",
    "gemini-url": "video_uri_submitted_not_fully_verified",
}


def _clip(text, length=26000):
    if len(text) > length:
        raise RuntimeError("VIDEO_PROMPT_TOO_LONG: reduce chunk size")
    return text


def _review_context(root, video_id, chunk):
    if type(chunk) is not int or chunk < 1:
        raise ValueError("VIDEO_REVIEW_CHUNK_REQUIRED")
    source, sha = ensure_source(video_dir(root, video_id))
    config = read_json(root / "config" / "agents.json")
    groups = split_segments(
        source["segments"], int(config.get("max_chars_per_chunk", 5500))
    )
    if chunk > len(groups):
        raise ValueError(f"VIDEO_CHUNK_OUT_OF_RANGE: total={len(groups)}")
    path = root / "research" / video_id / "timeline" / f"chunk_{chunk:03d}.md"
    if not path.is_file():
        raise RuntimeError("VIDEO_REVIEW_TIMELINE_MISSING")
    notes = path.read_text(encoding="utf-8")
    group = groups[chunk - 1]
    scope = {
        "source_sha256": sha,
        "chunk_sha256": digest(chunk_text(group)),
        "notes_sha256": digest(notes),
    }
    prompt_part = (
        f"[보완 대상 청크 {chunk}/{len(groups)}]\n"
        f"원문 시간 범위: {group[0]['start']}초 ~ "
        f"{group[-1]['start'] + group[-1].get('duration', 0)}초\n"
        "[원본 자막: 이 원문은 완전한 동영상 입력이 아닙니다]\n"
        + chunk_text(group) +
        "\n[기존 AGY 자막 분석: 동영상 검증 전 메모]\n" + notes
    )
    return f"review_chunk_{chunk:03d}", prompt_part, scope


def _prompt(video_id, mode, scope_text, provider):
    url = VIDEO_REF.format(video_id)
    head = (
        "[작업/증거 요구]\n"
        "YouTube URL을 단순히 추측하거나 검색 스니펫만으로 분석하지 마세요. "
        "사용할 수 있는 실제 웹/브라우저/비디오 도구로 소스를 열어 확인하세요. "
        "도구를 사용할 수 없거나 영상을 보지 못했다면 첫 줄에 "
        "'SOURCE_ACCESS_FAILED'를 쓰고 내용을 만들지 마세요. "
        "영상 페이지/자막만 읽었다면 프레임·화면·음성을 확인했다고 주장하지 마세요. "
        "프레임 확인/자막 확인/화면 미확인을 구분하세요.\n"
        f"정확한 대상 URL: {url}\n"
        "기술 정보와 사용법을 시간순으로 기록하고 각 항목에 실제 확인한 "
        "시간 위치(HH:MM:SS 또는 MM:SS)를 적으세요. "
        "원본에 없거나 화면에서 판독하지 못한 명령/버전은 추측하지 마세요.\n"
    )
    detail = (
        "자막 외에 화면 속 설정·노드 연결·명령/코드·누락된 실습 단계를 "
        "확인하고 보완하세요. 실제 영상을 확인할 수 없었다면 "
        "완전한 영상 검증이라고 표시하지 마세요.\n"
        if mode == "review" else
        "자막이 제공되지 않았습니다. 영상 자료를 직접 확인하여 "
        "시작부터 종료까지 시간순으로 최대한 상세한 기술 기록을 만드세요. "
        "영상을 확인할 수 없으면 모르는 내용을 채우지 마세요. "
        "자막 세그먼트 ID(s000001 등)는 존재하지 않으므로 생성 금지.\n"
    )
    contract = OUTPUT_CONTRACT if mode == "review" else (
        "[출력 형식] 실제 동영상 위치 시각만 사용하세요. "
        "원본 자막 세그먼트 ID는 전혀 없으므로 생성하지 마세요. "
        "시간순 세부 기술 내용과 실행 단계마다 시각을 적고, "
        "정말 확인되지 않는 것은 미확인이라고 명시하세요. "
        "모든 시간 구간을 다루지 못했다면 그 범위를 명시하세요.\n"
    )
    return _clip(head + detail + NEGATIVE_CONSTRAINTS +
                 ENTITY_TARGETS + contract + "\n" + scope_text)


def _agy_tool_evidence(stdout, video_id):
    """Only actual completed source-tool invocations count as observed access.

    Never infer media inspection from AGY's self-reported answer. Even a
    completed browser visit may have read only page text, not video frames.
    """
    observed = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") != "step_update":
            continue
        step = event.get("step_update") or {}
        if (step.get("step_type") != "tool"
                or step.get("state") != "DONE"):
            continue
        info = step.get("tool_info")
        info = info if isinstance(info, dict) else {}
        tool_name = step.get("tool_name") or info.get("name") or ""
        if not isinstance(tool_name, str) or info.get("error"):
            continue
        # A completed invoke_subagent step is delegation, NOT evidence that
        # the delegated worker fetched the URL. Require a direct web read.
        if not any(term in tool_name.lower() for term in (
            "read_url", "browser", "navigate",
        )):
            continue
        params = info.get("parameters") or {}
        # Tools can act on the wrong video. Validate the request target.
        if video_id not in json.dumps(params, ensure_ascii=False):
            continue
        observed.append(tool_name)
    return sorted(set(observed))


def probe_video_access(root: Path, video_id: str, *, runner=None):
    """Make one tiny, explicitly authorized AGY URL-read probe.

    Unlike full video processing this does not generate a research summary.
    The tool event is authoritative; terminal text alone is never proof.
    Archives raw CLI data privately and returns safe diagnostic metadata.
    """
    if not isinstance(video_id, str) or not VIDEO_RE.fullmatch(video_id):
        raise ValueError("VIDEO_ID_INVALID")
    runner = runner or Agy(root)
    config = runner.config
    model_cfg = config.get("video_extractor") or config["extractor"]
    model = model_cfg["model"]
    agent = "yt-research-source-probe"
    agent_file = root / ".agents" / "agents" / (agent + ".md")
    if not agent_file.is_file():
        raise RuntimeError("AGY_VIDEO_PROBE_AGENT_MISSING")
    prompt = (
        "Diagnose ONLY whether the read_url_content tool can open the following "
        "public YouTube URL. FIRST action: call read_url_content exactly once "
        "on the URL. Never infer access from the URL string. Never browse or "
        "summarize the video or retrieve captions separately. After the tool "
        "returns, output only SOURCE_READ_COMPLETED, or SOURCE_ACCESS_FAILED "
        "if the tool did not execute successfully. Do not include page contents. "
        f"URL: {VIDEO_REF.format(video_id)}"
    )
    command = [
        runner.binary, "-p", prompt,
        "--model", model, "--effort", model_cfg.get("effort", "medium"),
        "--agent", agent, "--output-format", "stream-json",
        "--print-timeout", "90s", "--disable-slash-commands",
    ]
    result = None
    error = None
    try:
        result = runner._invoke(command, "video_source_probe", 1, 1)
        if result.returncode != 0:
            error = RuntimeError("AGY_VIDEO_PROBE_NONZERO_EXIT")
        else:
            # A malformed envelope is an error, even though the archived
            # raw event stream is retained for private diagnostic inspection.
            try:
                envelope = parse_agy_envelope(result.stdout)
                if envelope.get("status") != "SUCCESS":
                    error = RuntimeError("AGY_VIDEO_PROBE_NON_SUCCESS")
            except (RuntimeError, ValueError, TypeError):
                error = RuntimeError("AGY_VIDEO_PROBE_INVALID_ENVELOPE")
    except (OSError, RuntimeError) as exc:
        error = RuntimeError("AGY_VIDEO_PROBE_EXECUTION_FAILED")
    finally:
        if result is not None:
            archive_agy_attempt(
                root, role="video_source", model=model, attempt=1,
                context={"video_id": video_id, "unit": "tool_probe"},
                stdout=result.stdout, stderr=result.stderr,
                exit_code=result.returncode, error=error, prompt=prompt,
            )
    if result is None:
        raise error
    summary = inspect_video_access_archive(root, video_id)
    # "SUCCESS" and the agent's response alone are not source-access proof.
    summary["probe_access_observed"] = bool(
        summary.get("successful_target_web_steps", 0)
    )
    summary["probe_cli_success"] = error is None
    return summary


def _agy_video(root, video_id, prompt, *, runner=None, context=None):
    """Run the tool-enabled video agent; no tool-free AGY transcript agent."""
    runner = runner or Agy(root)
    config = runner.config
    model_cfg = config.get("video_extractor") or config["extractor"]
    model = model_cfg["model"]
    name = config.get("agy_video_agent", "yt-research-video")
    agent_file = root / ".agents" / "agents" / f"{name}.md"
    if not agent_file.is_file():
        raise RuntimeError("AGY_VIDEO_AGENT_NOT_FOUND: dedicated tool-enabled agent required")
    cmd = [
        runner.binary, "-p", prompt,
        "--model", model, "--effort", model_cfg.get("effort", "medium"),
        "--agent", name, "--output-format", "stream-json",
        "--print-timeout", config.get("print_timeout", "10m"),
        "--disable-slash-commands",
    ]
    result = runner._invoke(cmd, "video_source", 1, 1)
    error = None
    try:
        if result.returncode != 0:
            raise RuntimeError("AGY_VIDEO_NONZERO_EXIT")
        envelope = parse_agy_envelope(result.stdout)
        if envelope.get("status") != "SUCCESS":
            raise RuntimeError("AGY_VIDEO_NON_SUCCESS")
        text = envelope.get("response")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("AGY_VIDEO_EMPTY")
        tools = _agy_tool_evidence(result.stdout, video_id)
        if not tools or text.lstrip().startswith("SOURCE_ACCESS_FAILED"):
            raise RuntimeError(
                "AGY_VIDEO_SOURCE_NOT_OBSERVED: 영상에 대한 직접 웹 도구 접근이 "
                "확인되지 않았습니다. 원인 진단: "
                f"python -m ytresearch diagnostics --video-id {video_id} "
                "--inspect-video-access (기존 저장 기록만 분석, AGY 재호출 없음)"
            )
        return text, {
            "provider": "agy", "model": model,
            "tool_names": tools,
            "access_status": ACCESS_STATES["agy"],
        }
    except (RuntimeError, ValueError, OSError) as exc:
        error = exc
        raise
    finally:
        if config.get("archive_agy_responses", True):
            try:
                archive_agy_attempt(
                    root, role="video_source", model=model,
                    attempt=1, context=context or {},
                    prompt=prompt, stdout=result.stdout,
                    stderr=result.stderr, exit_code=result.returncode,
                    error=error, duration_seconds=None,
                )
            except (OSError, ValueError, TypeError):
                report("[VIDEO] AGY raw archive could not be saved", error=True)


def _gemini_video(video_id, prompt, *, client=None, model="gemini-3.8-flash"):
    """Use the documented Gemini Interactions video URI input, not a text URL."""
    if client is None:
        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError(
                "GEMINI_API_KEY_MISSING: set an API key before using gemini-url"
            )
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "GOOGLE_GENAI_MISSING: python -m pip install google-genai"
            ) from exc
        client = genai.Client()
    result = client.interactions.create(
        model=model,
        input=[
            {"type": "text", "text": prompt},
            {"type": "video", "uri": VIDEO_REF.format(video_id)},
        ],
    )
    output = getattr(result, "output_text", None)
    if not isinstance(output, str) or not output.strip():
        raise RuntimeError("GEMINI_VIDEO_EMPTY_OR_UNAVAILABLE")
    if output.lstrip().startswith("SOURCE_ACCESS_FAILED"):
        raise RuntimeError("GEMINI_VIDEO_ACCESS_FAILED")
    return output, {
        "provider": "gemini-url", "model": model,
        "tool_names": [],
        "access_status": ACCESS_STATES["gemini-url"],
    }


def video_one(root: Path, video_id: str, *, mode="full", chunk=None,
              provider="agy", runner=None, client=None):
    """Extract full video without captions OR review a single subtitle chunk.

    New video evidence is supplemental; it cannot replace an immutable
    transcript or forge segment IDs. Research exports require source review.
    """
    if not isinstance(video_id, str) or not VIDEO_RE.fullmatch(video_id):
        raise ValueError("VIDEO_ID_INVALID")
    if mode not in ("full", "review") or provider not in ("agy", "gemini-url"):
        raise ValueError("VIDEO_MODE_OR_PROVIDER_INVALID")
    if mode == "review":
        unit, context_text, inputs = _review_context(root, video_id, chunk)
    elif chunk is not None:
        raise ValueError("VIDEO_FULL_DOES_NOT_TAKE_CHUNK")
    else:
        unit, context_text, inputs = "full", "", {}
    # Provider-specific evidence never overwrites or contaminates existing
    # AGY web-tool observations with Gemini's actual video-URI input.
    if provider == "gemini-url":
        unit += "_gemini"
    prompt = _prompt(video_id, mode, context_text, provider)
    folder = root / "research" / video_id / VIDEO_EVIDENCE
    text_file = folder / f"{unit}.md"
    meta_file = folder / f"{unit}.json"
    signature = {
        "video_id": video_id, "unit": unit, "mode": mode,
        "provider": provider, "prompt_sha256": digest(prompt), **inputs,
    }
    if text_file.exists() != meta_file.exists():
        raise RuntimeError("VIDEO_INCOMPLETE_CHECKPOINT")
    if meta_file.exists():
        meta = read_json(meta_file)
        text = text_file.read_text(encoding="utf-8")
        if (any(meta.get(k) != v for k, v in signature.items())
                or digest(text) != meta.get("text_sha256")):
            raise RuntimeError("VIDEO_CHECKPOINT_MISMATCH")
        report(f"[VIDEO] {video_id} {unit}: existing checkpoint reused")
        return meta
    context = {"video_id": video_id, "unit": unit, "mode": mode}
    if provider == "agy":
        text, info = _agy_video(
            root, video_id, prompt, runner=runner, context=context,
        )
    else:
        config = read_json(root / "config" / "agents.json")
        text, info = _gemini_video(
            video_id, prompt, client=client,
            model=config.get("gemini_video_model", "gemini-3.8-flash"),
        )
    # Reject hallucinated segment citations for direct-video-only collection.
    if mode == "full" and re.search(r"(?<![A-Za-z0-9_-])s\d{6}(?![A-Za-z0-9_-])", text):
        raise RuntimeError("VIDEO_UNGROUNDED_SEGMENT_IDS")
    if not re.search(r"(?<!\d)(?:\d{1,2}:)?\d{1,2}:\d{2}(?!\d)", text):
        raise RuntimeError("VIDEO_TIMESTAMPS_NOT_PRESENT")
    meta = {
        **signature, **info, "text_sha256": digest(text),
        "review_required": True,
        "note": (
            "Generated from a tool-researched URL or a submitted video URI. "
            "Do not assert full audiovisual verification or transcript parity "
            "without independent source inspection."
        ),
    }
    title = (
        f"# Video {mode} evidence — {video_id} / {unit}\n\n"
        f"Source: {VIDEO_REF.format(video_id)}\n\n"
        f"Provider: {provider}; access: {info['access_status']}\n\n"
        "This is model-generated evidence under review, NOT a verified "
        "complete transcript. No source segment IDs may be fabricated.\n\n"
        "---\n\n"
    )
    body = title + text
    meta["text_sha256"] = digest(body)
    atomic_write(text_file, body, immutable=True)
    atomic_write(meta_file, json_text(meta), immutable=True)
    report(f"[VIDEO] {video_id} {unit}: saved, requires ChatGPT/source review")
    return meta


def compile_reviewed_timeline(root: Path, video_id: str, *, provider="agy"):
    """Integrate subtitle notes and each video review without overwriting either.

    This is a complete *set of chunks*, not a guarantee that AGY saw every
    visual frame. Each chunk retains separate original and observed evidence.
    """
    source, source_sha = ensure_source(video_dir(root, video_id))
    config = read_json(root / "config" / "agents.json")
    groups = split_segments(
        source["segments"], int(config.get("max_chars_per_chunk", 5500))
    )
    folder = root / "research" / video_id / VIDEO_EVIDENCE
    sections, proofs = [], []
    for number, group in enumerate(groups, 1):
        unit = f"review_chunk_{number:03d}"
        if provider == "gemini-url":
            unit += "_gemini"
        meta_file, review_file = folder / (unit + ".json"), folder / (unit + ".md")
        if not meta_file.is_file() or not review_file.is_file():
            return None
        meta = read_json(meta_file)
        review = review_file.read_text(encoding="utf-8")
        original_file = root / "research" / video_id / "timeline" / f"chunk_{number:03d}.md"
        original = original_file.read_text(encoding="utf-8")
        if (meta.get("video_id") != video_id
                or meta.get("source_sha256") != source_sha
                or meta.get("chunk_sha256") != digest(chunk_text(group))
                or meta.get("notes_sha256") != digest(original)
                or meta.get("text_sha256") != digest(review)):
            raise RuntimeError("VIDEO_REVIEW_EVIDENCE_MISMATCH")
        sections.append(
            f"## 청크 {number}/{len(groups)} — "
            f"{group[0]['start']:.2f}s ~ "
            f"{group[-1]['start'] + group[-1].get('duration', 0):.2f}s\n\n"
            "### 자막 기반 초기 시간순 기록 (보조, 미검증)\n\n"
            + original +
            "\n\n### 영상 접근 기반 보완 관찰 (별도 검수 필요)\n\n"
            + review + "\n"
        )
        proofs.append({
            "unit": unit, "original_sha256": digest(original),
            "review_sha256": digest(review),
            "access_status": meta.get("access_status"),
        })
    combined = (
        f"# Integrated chronological evidence — {video_id}\n\n"
        f"Source: {VIDEO_REF.format(video_id)}\n\n"
        f"Coverage: {len(groups)} / {len(groups)} subtitle chunks with "
        "independent AGY/Gemini source-check notes.\n\n"
        "Important: chunk coverage is NOT proof of complete audiovisual access "
        "or factual correctness. The original transcript is the primary text "
        "source; individual video checks may have inspected web captions "
        "instead of actual moving frames. ChatGPT Web must reconcile both.\n\n"
        + "\n\n".join(sections)
    )
    target_unit = "combined_timeline_gemini" if provider == "gemini-url" else "combined_timeline"
    output = folder / (target_unit + ".md")
    manifest = {
        "video_id": video_id, "source_sha256": source_sha,
        "chunk_count": len(groups), "chunks": proofs,
        "text_sha256": digest(combined),
        "review_required": True,
        "scope": "subtitle_chunk_coverage_not_guaranteed_audiovisual_coverage",
    }
    atomic_write(output, combined, immutable=True)
    atomic_write(folder / (target_unit + ".json"), json_text(manifest),
                 immutable=True)
    report(
        f"[VIDEO] {video_id}: {len(groups)}개 청크 통합 타임라인 생성 "
        "(사실 검수는 ChatGPT Web 필요)"
    )
    return output


def review_next(root: Path, video_id: str, *, provider="agy",
                max_chunks=1, runner=None, client=None):
    """Incrementally review full subtitle-based timeline with direct video."""
    source, _ = ensure_source(video_dir(root, video_id))
    config = read_json(root / "config" / "agents.json")
    count = len(split_segments(
        source["segments"], int(config.get("max_chars_per_chunk", 5500))
    ))
    completed = []
    for chunk in range(1, count + 1):
        unit = f"review_chunk_{chunk:03d}"
        if provider == "gemini-url":
            unit += "_gemini"
        meta_file = root / "research" / video_id / VIDEO_EVIDENCE / (unit + ".json")
        if meta_file.is_file():
            continue
        if len(completed) >= max_chunks:
            break
        video_one(root, video_id, mode="review", chunk=chunk,
                  provider=provider, runner=runner, client=client)
        completed.append(chunk)
    compile_reviewed_timeline(root, video_id, provider=provider)
    report(
        f"[VIDEO] review {video_id}: 신규 {len(completed)}개 / 전체 {count}개. "
        "각 리뷰는 추가 영상 증거이며 원본 AGY notes를 덮어쓰지 않습니다."
    )
    return completed


def evidence_status(root: Path, video_id: str):
    """Report video-only research without implying a canonical transcript."""
    dest = root / "research" / video_id / VIDEO_EVIDENCE
    # Explicit video input is a stronger path than a fetched YouTube web page.
    for unit in ("full_gemini", "full"):
        meta_file = dest / (unit + ".json")
        document = dest / (unit + ".md")
        if meta_file.is_file() != document.is_file():
            raise RuntimeError("VIDEO_EVIDENCE_INCOMPLETE")
        if not meta_file.is_file():
            continue
        meta = read_json(meta_file)
        if (meta.get("video_id") != video_id
                or meta.get("unit") != unit
                or digest(document.read_text(encoding="utf-8")) !=
                meta.get("text_sha256")):
            raise RuntimeError("VIDEO_EVIDENCE_TAMPERED")
        return {
            "status": "video_evidence_review_required",
            "video_evidence": f"{video_id}/video_evidence/{unit}.md",
            "source_access": meta.get("access_status"),
        }
    return None
