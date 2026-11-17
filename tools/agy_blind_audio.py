"""Offline-generated blinded Korean speech vs silence evaluation for AGY Flash.

No answer key is placed in an AGY workspace or public Git checkout. This tool
does NOT claim that an AGY read_file tool call independently proves audio access.
External tools (Windows Korean SAPI, ffmpeg, agy) run only on explicit commands.
"""
from __future__ import annotations

import argparse
from array import array
import math
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import wave

MODEL = "gemini-3.8-flash-medium"
ACOUSTIC_CONTRACT = "host_terminal_json_v3"
ACOUSTIC_CLASSES = frozenset({"SPEECH", "TONE", "SILENCE", "UNAVAILABLE"})
TONE_COUNTS = range(2, 6)

DIGITS = "영일이삼사오육칠팔구"
STARTS = ("파란 우산이", "작은 풍선이", "노란 연필이", "하얀 구름이",
          "조용한 기차가", "빨간 자전거가", "은빛 물고기가", "초록 나무가")
MIDDLES = ("오래된 다리 옆에서", "맑은 강물 앞에서", "넓은 광장 가운데에서",
           "아침 햇살 아래에서", "조용한 도서관 근처에서", "높은 언덕 너머에서")
ENDS = ("잠시 멈췄습니다.", "천천히 움직였습니다.", "다시 방향을 바꿨습니다.",
        "작은 소리를 냈습니다.", "세 번 고개를 끄덕였습니다.")
PROMPT = (
    "작업 디렉터리에 있는 clip_00_30.mp3 파일만 read_file 도구로 직접 읽고 "
    "실제 들리는 한국어 발화를 정확하게 전사하세요. 소리가 없으면 발화를 만들어내지 마세요. "
    "비프음이나 신호음이 들려도 사람의 발화로 전사하지 마세요. "
    "사람의 발화, 비프음, 완전 무음, 오디오 접근 불가를 구별하세요. "
    "비프음이 들리면 정확한 비프 횟수를 세세요. 파일명과 메타데이터로 추측하지 마세요. "
    "파일 경로 및 메타데이터만 받았으면 NO_AUDIO_ACCESS로 보고하세요. "
    "외부 파일, 웹 검색, 실행 명령, 음성 인식 프로그램, 참고 자막을 사용하지 마세요. "
    "파일을 생성하거나 수정하지 마세요. write_file, write_to_file, 실행 명령을 사용하지 마세요. "
    "오직 최종 답변에 UTF-8 JSON 객체 한 개만 출력하세요. 마크다운이나 설명을 넣지 마세요. "
    "응답은 실행기가 비공개 폴더에 보관합니다. "
    '스키마: {"verdict":"AUDIO_TRANSCRIBED|NO_SPEECH|NO_AUDIO_ACCESS",'
    '"audio_analyzed":true,"acoustic_class":"SPEECH|TONE|SILENCE|UNAVAILABLE",'
    '"beep_count":0,"segments":[{"start":0.0,"end":2.0,"text":"실제 발화"}]}. '
    "말이 들리지 않으면 segments=[], verdict=NO_SPEECH입니다. "
    "비프음만 들리면 acoustic_class=TONE, beep_count=실제 횟수입니다. "
    "완전 무음이면 acoustic_class=SILENCE, beep_count=0입니다. "
    "발화가 들리면 acoustic_class=SPEECH, beep_count=0입니다. "
    "오디오 자체에 접근하지 못했다면 acoustic_class=UNAVAILABLE, beep_count=null, "
    "audio_analyzed=false, segments=[], "
    "verdict=NO_AUDIO_ACCESS입니다. 예시는 정답이 아닙니다."
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite: {path}")
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _default_root(name: str) -> Path:
    home = os.environ.get("LOCALAPPDATA")
    if not home:
        raise RuntimeError("LOCALAPPDATA unavailable; provide explicit root paths")
    return Path(home) / "AGYBlindAudio" / name


def _ensure_roots(public_root: Path, private_root: Path) -> tuple[Path, Path]:
    public_root, private_root = public_root.resolve(), private_root.resolve()
    repo = Path(__file__).resolve().parents[1]
    if (public_root == private_root or public_root.is_relative_to(private_root)
            or private_root.is_relative_to(public_root)
            or public_root.is_relative_to(repo) or private_root.is_relative_to(repo)):
        raise ValueError("Runtime and private roots must be separate and outside the Git checkout")
    return public_root, private_root


def new_phrases(n: int) -> list[dict]:
    rng = secrets.SystemRandom()
    phrases = []
    seen = set()
    for _ in range(n):
        while True:
            code = "".join(rng.choice(DIGITS) for _ in range(6))
            phrase = (f"{rng.choice(STARTS)} {rng.choice(MIDDLES)} "
                      f"{rng.choice(ENDS)} 확인 번호는 {' '.join(code)} 입니다.")
            if phrase not in seen:
                seen.add(phrase)
                phrases.append({"kind": "speech", "reference": phrase, "nonce": code})
                break
    return phrases


def plan(speech_count: int = 3, silence_count: int = 2,
         tone_count: int = 0) -> list[dict]:
    """Legacy 2-condition caller remains compatible; new CLI creates 3 conditions."""
    if (not 1 <= speech_count <= 10 or not 1 <= silence_count <= 10
            or not 0 <= tone_count <= 10):
        raise ValueError("speech/silence must be 1..10 and tone 0..10")
    rng = secrets.SystemRandom()
    cases = new_phrases(speech_count) + [
        {"kind": "tone", "reference": "", "nonce": "",
         "beep_count": rng.choice(tuple(TONE_COUNTS))}
        for _ in range(tone_count)
    ] + [
        {"kind": "silence", "reference": "", "nonce": ""}
        for _ in range(silence_count)
    ]
    rng.shuffle(cases)
    return cases


def _wav_voice_ok(path: Path) -> None:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise ValueError("SAPI output must be mono 16-bit WAV")
        if wav.getnframes() < wav.getframerate() * 2:
            raise ValueError("Spoken clip is too short")
        if wav.getnframes() > wav.getframerate() * 29:
            raise ValueError("Spoken clip exceeds the 30-second trial window")
        samples = array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        if max(map(abs, samples), default=0) < 150:
            raise ValueError("SAPI generated near-silent speech; aborting")


def _synthesize_tone_wav(path: Path, beep_count: int) -> None:
    """Create 30 s PCM with audible 920 Hz beeps and a secret random count."""
    if type(beep_count) is not int or beep_count not in TONE_COUNTS:
        raise ValueError("Invalid beep count")
    rate, total_samples = 16000, 30 * 16000
    beep_samples, fade_samples = int(rate * 0.52), int(rate * 0.01)
    zero_chunk = b"\x00\x00" * rate
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        position = 0
        for i in range(beep_count):
            start = int(rate * (0.8 + i * 1.15))
            remaining = start - position
            while remaining:
                take = min(remaining, rate)
                wav.writeframesraw(zero_chunk[:take * 2])
                remaining -= take
            samples = array("h")
            for j in range(beep_samples):
                fade = min(1.0, j / fade_samples,
                           (beep_samples - 1 - j) / fade_samples)
                samples.append(round(10500 * fade * math.sin(
                    2 * math.pi * 920 * j / rate)))
            if sys.byteorder != "little":
                samples.byteswap()
            wav.writeframesraw(samples.tobytes())
            position = start + beep_samples
        remaining = total_samples - position
        while remaining:
            take = min(remaining, rate)
            wav.writeframesraw(zero_chunk[:take * 2])
            remaining -= take


def _encode_mp3(out: Path, phrase: str | None, private_dir: Path,
                *, beep_count: int | None = None) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe must be on PATH")
    if out.exists():
        raise FileExistsError(out)
    wav_path = None
    try:
        if beep_count is not None:
            if phrase is not None:
                raise ValueError("Tone trial may not contain speech")
            wav_path = private_dir / ("tone-" + secrets.token_hex(8) + ".wav")
            _synthesize_tone_wav(wav_path, beep_count)
            args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", str(wav_path), "-t", "30"]
        elif phrase is not None:
            if os.name != "nt":
                raise RuntimeError("Windows Korean SAPI voice is required for real speech generation")
            wav_path = private_dir / ("voice-" + secrets.token_hex(8) + ".wav")
            ps = Path(__file__).resolve().parents[1] / "scripts" / "synthesize-ko-sapi.ps1"
            process = subprocess.run([
                "powershell.exe", "-NoLogo", "-NoProfile", "-STA",
                "-ExecutionPolicy", "RemoteSigned", "-File", str(ps),
                "-OutputWav", str(wav_path)],
                input=phrase.encode("utf-8"), capture_output=True, timeout=90)
            if process.returncode != 0:
                raise RuntimeError("Korean SAPI generation failed; check installed ko-KR voice")
            _wav_voice_ok(wav_path)
            args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", str(wav_path), "-af", "apad", "-t", "30"]
        else:
            args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "30"]
        args += ["-vn", "-ar", "16000", "-ac", "1", "-c:a", "libmp3lame",
                 "-b:a", "96k", "-y", str(out)]
        subprocess.run(args, check=True, capture_output=True, timeout=90)
        spec = json.loads(subprocess.run([
            ffprobe, "-v", "error", "-of", "json",
            "-show_entries", "stream=codec_name:format=duration",
            str(out)], check=True, capture_output=True, text=True, timeout=20).stdout)
        duration = float(spec["format"]["duration"])
        if not 29.5 <= duration <= 30.5 or not any(
                s.get("codec_name") == "mp3" for s in spec["streams"]):
            raise ValueError("Failed to produce a validated 30-second MP3")
    except Exception:
        out.unlink(missing_ok=True)
        raise
    finally:
        if wav_path is not None:
            wav_path.unlink(missing_ok=True)


def prepare(runtime_root: Path, private_root: Path,
            speech_count: int = 3, silence_count: int = 1,
            tone_count: int = 1) -> dict:
    runtime_root, private_root = _ensure_roots(runtime_root, private_root)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg/ffprobe are required")
    session = "blind-" + uuid.uuid4().hex[:16]
    pub, priv = runtime_root / session, private_root / session
    pub.mkdir(parents=True, exist_ok=False)
    priv.mkdir(parents=True, exist_ok=False)
    records = []
    for item in plan(speech_count, silence_count, tone_count):
        case_id = "case-" + uuid.uuid4().hex[:12]
        workspace = pub / case_id
        workspace.mkdir(parents=True)
        media = workspace / "clip_00_30.mp3"
        if item["kind"] == "tone":
            _encode_mp3(media, None, priv, beep_count=item["beep_count"])
        else:
            _encode_mp3(media, item["reference"] if item["kind"] == "speech" else None, priv)
        records.append({
            "id": case_id, "kind": item["kind"], "reference": item["reference"],
            "nonce": item["nonce"], "beep_count": item.get("beep_count", 0),
            "sha256": _sha(media),
        })
    manifest = {"schema": "agy_blind_audio_v2", "session": session,
                "runtime_root": str(pub), "cases": records}
    _json(priv / "manifest.json", manifest)
    return {"session": session, "trial_count": len(records),
            "runtime": str(pub), "private_manifest": str(priv / "manifest.json"),
            "model_called": False}


def _load_session(private_root: Path, session: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"blind-[0-9a-f]{16}", session):
        raise ValueError("Invalid session ID")
    root = private_root.resolve() / session
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") not in {"agy_blind_audio_v1", "agy_blind_audio_v2"} or manifest.get("session") != session:
        raise ValueError("Invalid manifest")
    return root, manifest


def run_trial(private_root: Path, session: str, case_id: str, submit: bool,
              auto_approve: bool = False, timeout_seconds: int = 240) -> dict:
    priv, manifest = _load_session(private_root, session)
    match = [c for c in manifest["cases"] if c["id"] == case_id]
    if len(match) != 1:
        raise ValueError("Unknown trial id")
    workspace = Path(manifest["runtime_root"]) / case_id
    audio = workspace / "clip_00_30.mp3"
    if not audio.is_file() or _sha(audio) != match[0]["sha256"]:
        raise ValueError("Audio hash mismatch: refusing AGY request")
    answer = workspace / "answer.json"
    log = priv / (case_id + ".ndjson")
    if not submit:
        return {"trial": case_id, "submitted": False, "model": MODEL,
                "previous_attempt_present": answer.exists() or log.exists()}
    if answer.exists() or log.exists():
        raise FileExistsError("Trial already attempted; original evidence must not be overwritten")
    if os.name != "nt":
        raise RuntimeError("Real AGY trials require Windows")
    agy = shutil.which("agy") or shutil.which("agy.cmd")
    if not agy:
        raise RuntimeError("agy CLI not found")
    command = [agy, "-p", PROMPT, "--model", MODEL, "--effort", "medium",
               "--output-format", "stream-json", "--print-timeout",
               str(timeout_seconds) + "s", "--sandbox", "--disable-slash-commands"]
    if auto_approve:
        command.append("--dangerously-skip-permissions")
    status_file = priv / (case_id + ".status.json")
    with log.open("xb") as stdout, (priv / (case_id + ".stderr")).open("xb") as stderr:
        try:
            proc = subprocess.run(command, cwd=workspace, stdout=stdout,
                                  stderr=stderr, timeout=timeout_seconds + 20,
                                  check=False)
            status = {"submitted": True, "exit_code": proc.returncode,
                      "timed_out": False, "auto_approve": auto_approve,
                      "output_contract": (ACOUSTIC_CONTRACT
                          if manifest["schema"] == "agy_blind_audio_v2"
                          else "host_terminal_json_v2")}
        except subprocess.TimeoutExpired:
            status = {"submitted": True, "exit_code": None,
                      "timed_out": True, "auto_approve": auto_approve,
                      "output_contract": (ACOUSTIC_CONTRACT
                          if manifest["schema"] == "agy_blind_audio_v2"
                          else "host_terminal_json_v2")}
    _json(status_file, status)
    # The model must not write answer.json. Any unexpected model-created answer
    # is retained as evidence but cannot make a clean read-only trial pass.
    recovery = recover_case(priv, manifest, match[0]) if (
        not answer.exists() and status["exit_code"] == 0
        and not status["timed_out"]) else {"recovery": "NOT_ELIGIBLE"}
    return {"trial": case_id, "submitted": True, "exit_code": status["exit_code"],
            "timed_out": status["timed_out"],
            "answer_present": answer.is_file(),
            "answer_recovered": recovery["recovery"] == "RECOVERED_FROM_TERMINAL_JSON",
            "recovery_status": recovery["recovery"],
            "raw_cli_log_private": True}


def inspect_headless_trace(path: Path) -> dict:
    """Read-only model provenance: require target read_file; reject any writes."""
    reads = set()
    forbidden = set()
    result_success = False
    if not path.exists():
        return {"mp3_read_call_observed": False, "other_tool_calls_observed": False,
                "terminal_success_observed": False}
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            event = row.get("event")
            if event == "result":
                final = row.get("result")
                if isinstance(final, dict) and final.get("status") == "SUCCESS":
                    result_success = True
            if event != "step_update":
                continue
            step = row.get("step_update")
            if not isinstance(step, dict) or step.get("step_type") != "tool":
                continue
            info = step.get("tool_info")
            if not isinstance(info, dict):
                continue
            name = info.get("name") or step.get("tool_name")
            if not isinstance(name, str):
                continue
            name = name.split(".")[-1]
            index = step.get("step_index")
            if name == "read_file":
                parameters = info.get("parameters")
                serialized = json.dumps(parameters, ensure_ascii=False)
                if "clip_00_30.mp3" in serialized.casefold():
                    reads.add(index)
                else:
                    forbidden.add(index)
            else:
                # The host alone persists model output. Every additional tool,
                # including write_file, invalidates the blind-trial provenance.
                forbidden.add(index)
    return {
        "mp3_read_call_observed": bool(reads),
        "other_tool_calls_observed": bool(forbidden),
        "terminal_success_observed": result_success,
    }


def _parse_terminal_json_response(
        raw_response: str, *, contract: str | None = None) -> dict | None:
    """Parse ONLY the final model response, never intermediary tool/progress text."""
    if not isinstance(raw_response, str) or not 1 <= len(raw_response) <= 100000:
        return None
    text = raw_response.strip()
    fenced = re.fullmatch(r"\x60{3}(?:json)?\s*\n([\s\S]*?)\n\x60{3}", text,
                          flags=re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if value.get("verdict") not in {"AUDIO_TRANSCRIBED", "NO_SPEECH",
                                    "NO_AUDIO_ACCESS"}:
        return None
    if type(value.get("audio_analyzed")) is not bool:
        return None
    segments = value.get("segments")
    if not isinstance(segments, list) or len(segments) > 100:
        return None
    if any(not isinstance(seg, dict)
           or not isinstance(seg.get("text"), str)
           or len(seg["text"]) > 2000
           or not isinstance(seg.get("start"), (float, int))
           or not isinstance(seg.get("end"), (float, int))
           or isinstance(seg.get("start"), bool)
           or isinstance(seg.get("end"), bool)
           or not 0 <= seg["start"] < seg["end"] <= 30.5
           for seg in segments):
        return None
    if value["verdict"] == "AUDIO_TRANSCRIBED":
        if not value["audio_analyzed"] or not segments or not any(
                seg["text"].strip() for seg in segments):
            return None
    elif value["verdict"] == "NO_SPEECH":
        if not value["audio_analyzed"] or segments:
            return None
    else:
        if value["audio_analyzed"] or segments:
            return None
    # Return only an allowlisted, validated subset. Never publish raw response.
    canonical = {"verdict": value["verdict"], "audio_analyzed": value["audio_analyzed"],
                 "segments": [{"start": seg["start"], "end": seg["end"],
                               "text": seg["text"]} for seg in segments]}
    if contract == ACOUSTIC_CONTRACT:
        acoustic_class = value.get("acoustic_class")
        beep_count = value.get("beep_count")
        if not isinstance(acoustic_class, str) or acoustic_class not in ACOUSTIC_CLASSES:
            return None
        if (value["verdict"] == "AUDIO_TRANSCRIBED" and not
                (acoustic_class == "SPEECH" and type(beep_count) is int
                 and beep_count == 0)):
            return None
        if value["verdict"] == "NO_SPEECH" and not (
            acoustic_class == "TONE" and type(beep_count) is int
            and beep_count in TONE_COUNTS or
            acoustic_class == "SILENCE" and type(beep_count) is int
            and beep_count == 0
        ):
            return None
        if value["verdict"] == "NO_AUDIO_ACCESS" and not (
            acoustic_class == "UNAVAILABLE" and beep_count is None
        ):
            return None
        canonical["acoustic_class"] = acoustic_class
        canonical["beep_count"] = beep_count
    return canonical


def _categorize_response_validation(raw_response: str,
                                    contract: str) -> dict:
    """Diagnose final JSON shape without returning content or secret values.

    Categories are evidence about the response contract, not whether audio
    reached the model. Never echo text, unknown keys, dynamic enum strings,
    model utterances, audio labels from the private answer key, or JSON errors.
    """
    result = {
        "validation_category": "UNKNOWN",
        "response_character_count": (
            len(raw_response) if isinstance(raw_response, str) else 0),
        "reported_verdict": "UNKNOWN_OR_MISSING",
        "reported_acoustic_class": "UNKNOWN_OR_MISSING",
        "reported_audio_analyzed": None,
        "reported_beep_count_type": "MISSING",
        "reported_segments_count": None,
        "response_envelope": "UNKNOWN",
        "independent_audio_receipt_verified": False,
    }
    if not isinstance(raw_response, str) or not raw_response.strip():
        result["validation_category"] = "EMPTY_TERMINAL_RESPONSE"
        return result
    if len(raw_response) > 100000:
        result["validation_category"] = "OVERSIZED_TERMINAL_RESPONSE"
        return result
    content = raw_response.strip()
    fence = re.fullmatch(r"\x60{3}(?:json)?\s*\n([\s\S]*?)\n\x60{3}",
                         content, flags=re.IGNORECASE)
    if fence:
        result["response_envelope"] = "STANDALONE_JSON_FENCE"
        content = fence.group(1).strip()
    elif content.startswith("\x60\x60\x60"):
        result["response_envelope"] = "UNEXPECTED_MARKDOWN_ENVELOPE"
        result["validation_category"] = "NON_STANDALONE_JSON_TEXT"
        return result
    else:
        result["response_envelope"] = "PLAIN_TEXT"
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        result["validation_category"] = "INVALID_JSON_SYNTAX_OR_ADDITIONAL_TEXT"
        return result
    if not isinstance(obj, dict):
        result["validation_category"] = "JSON_TOP_LEVEL_NOT_OBJECT"
        return result
    verdict = obj.get("verdict")
    if isinstance(verdict, str) and verdict in {
            "AUDIO_TRANSCRIBED", "NO_SPEECH", "NO_AUDIO_ACCESS"}:
        result["reported_verdict"] = verdict
    else:
        result["validation_category"] = "VERDICT_MISSING_OR_INVALID"
        return result
    analyzed = obj.get("audio_analyzed")
    if type(analyzed) is not bool:
        result["validation_category"] = "AUDIO_ANALYZED_NOT_BOOLEAN"
        return result
    result["reported_audio_analyzed"] = analyzed
    segs = obj.get("segments")
    if not isinstance(segs, list) or len(segs) > 100:
        result["validation_category"] = "SEGMENTS_NOT_VALID_LIST"
        return result
    result["reported_segments_count"] = len(segs)
    if any(not isinstance(seg, dict)
           or not isinstance(seg.get("text"), str)
           or len(seg["text"]) > 2000
           or type(seg.get("start")) not in {float, int}
           or type(seg.get("end")) not in {float, int}
           or not 0 <= seg["start"] < seg["end"] <= 30.5
           for seg in segs):
        result["validation_category"] = "SEGMENT_ELEMENT_SCHEMA_INVALID"
        return result
    if verdict == "AUDIO_TRANSCRIBED":
        if not analyzed or not segs or not any(x["text"].strip() for x in segs):
            result["validation_category"] = "VERDICT_SEGMENTS_OR_ANALYSIS_CONFLICT"
            return result
    elif (verdict == "NO_SPEECH" and (not analyzed or segs)
          or verdict == "NO_AUDIO_ACCESS" and (analyzed or segs)):
        result["validation_category"] = "VERDICT_SEGMENTS_OR_ANALYSIS_CONFLICT"
        return result
    if contract == ACOUSTIC_CONTRACT:
        acoustic = obj.get("acoustic_class")
        if isinstance(acoustic, str) and acoustic in ACOUSTIC_CLASSES:
            result["reported_acoustic_class"] = acoustic
        else:
            result["validation_category"] = "ACOUSTIC_CLASS_MISSING_OR_INVALID"
            return result
        beep = obj.get("beep_count", ...)
        result["reported_beep_count_type"] = (
            "MISSING" if beep is ... else "NULL" if beep is None
            else "BOOLEAN" if type(beep) is bool
            else "INTEGER" if type(beep) is int else "OTHER")
        matched = (
            verdict == "AUDIO_TRANSCRIBED" and acoustic == "SPEECH"
            and type(beep) is int and beep == 0
            or verdict == "NO_SPEECH" and (
                acoustic == "TONE" and type(beep) is int and beep in TONE_COUNTS
                or acoustic == "SILENCE" and type(beep) is int and beep == 0)
            or verdict == "NO_AUDIO_ACCESS" and
            acoustic == "UNAVAILABLE" and beep is None
        )
        if not matched:
            result["validation_category"] = "ACOUSTIC_VERDICT_OR_BEEP_COUNT_CONFLICT"
            return result
    if _parse_terminal_json_response(raw_response, contract=contract) is None:
        result["validation_category"] = "PARSER_REJECTED_OTHER"
    else:
        result["validation_category"] = "VALID_TERMINAL_JSON"
    return result


def diagnose_terminal_responses(private_root: Path, session: str) -> dict:
    """Inspect archived headless responses only; never contact AGY or write files."""
    priv, manifest = _load_session(private_root, session)
    reports = []
    for item in manifest["cases"]:
        case_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(case_id, str) or not re.fullmatch(
                r"case-[0-9a-f]{12}", case_id):
            raise ValueError("Invalid trial identifier in private manifest")
        status_path = priv / (case_id + ".status.json")
        try:
            if status_path.is_symlink():
                raise ValueError("Symlink status is not allowed")
            obj = json.loads(status_path.read_text(encoding="utf-8"))
            contract = obj.get("output_contract")
        except (OSError, ValueError, TypeError):
            contract = None
        safe_contract = contract if contract in {
            "host_terminal_json_v2", ACOUSTIC_CONTRACT,
            "legacy_workspace_answer"} else "UNKNOWN_CONTRACT"
        trace = priv / (case_id + ".ndjson")
        if trace.is_symlink() or (trace.exists() and trace.stat().st_size >
                                 25 * 1024 * 1024):
            reports.append({"trial": case_id,
                            "validation_category": "UNSAFE_OR_OVERSIZED_TRACE"})
            continue
        state, response = _terminal_response_from_stream(trace)
        if response is None:
            reports.append({"trial": case_id,
                            "output_contract": safe_contract,
                            "validation_category": state
                            if state in {
                                "TRACE_MISSING", "NO_UNIQUE_TERMINAL_RESULT",
                                "TERMINAL_NOT_SUCCESS", "EMPTY_TERMINAL_RESPONSE"}
                            else "UNKNOWN_TERMINAL_STATE"})
            continue
        report = _categorize_response_validation(response, safe_contract)
        reports.append({"trial": case_id, "output_contract": safe_contract,
                        **report})
    return {"schema": "agy_blind_terminal_validation_diagnostic_v1",
            "session": session, "model_called": False,
            "all_responses_private": True, "trials": reports,
            "note": "Classifies terminal response schema only; never scores secret audio content."}


def _terminal_response_from_stream(path: Path) -> tuple[str, str | None]:
    """Return final successful model response, not a tool message or stream delta."""
    if not path.exists():
        return "TRACE_MISSING", None
    terminals = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("event") == "result":
                event = obj.get("result")
                if isinstance(event, dict):
                    terminals.append(event)
    if len(terminals) != 1:
        return "NO_UNIQUE_TERMINAL_RESULT", None
    event = terminals[0]
    if event.get("status") != "SUCCESS":
        return "TERMINAL_NOT_SUCCESS", None
    response = event.get("response")
    if not isinstance(response, str) or not response.strip():
        return "EMPTY_TERMINAL_RESPONSE", None
    return "TERMINAL_RESPONSE_PRESENT", response


def recover_case(priv: Path, manifest: dict, item: dict) -> dict:
    """Recover already-saved terminal JSON only, without calling any model."""
    case_id = item["id"]
    ws = Path(manifest["runtime_root"]) / case_id
    original = ws / "answer.json"
    recovered = priv / "recoveries" / (case_id + ".response.json")
    if original.exists():
        return {"trial": case_id, "recovery": "ORIGINAL_ANSWER_PRESENT"}
    if recovered.exists():
        return {"trial": case_id, "recovery": "ALREADY_RECOVERED"}
    media = ws / "clip_00_30.mp3"
    if not media.is_file() or _sha(media) != item["sha256"]:
        return {"trial": case_id, "recovery": "MEDIA_HASH_MISMATCH"}
    status_file = priv / (case_id + ".status.json")
    try:
        status = json.loads(status_file.read_text(encoding="utf-8"))
        if (status.get("submitted") is not True
                or status.get("exit_code") != 0
                or status.get("timed_out") is not False):
            return {"trial": case_id, "recovery": "CLI_NOT_COMPLETED"}
    except (OSError, ValueError):
        return {"trial": case_id, "recovery": "CLI_STATUS_MISSING"}
    trace = priv / (case_id + ".ndjson")
    provenance = inspect_headless_trace(trace)
    if not provenance["terminal_success_observed"]:
        return {"trial": case_id, "recovery": "TERMINAL_NOT_SUCCESS"}
    if not provenance["mp3_read_call_observed"]:
        return {"trial": case_id, "recovery": "NO_MP3_READ_FILE_CALL"}
    if provenance["other_tool_calls_observed"]:
        return {"trial": case_id, "recovery": "DISALLOWED_TOOL_CALL"}
    state, response = _terminal_response_from_stream(trace)
    if response is None:
        return {"trial": case_id, "recovery": state}
    obj = _parse_terminal_json_response(
        response, contract=status.get("output_contract"))
    if obj is None:
        return {"trial": case_id, "recovery": "UNPARSEABLE_TERMINAL_JSON",
                "terminal_response_length": len(response),
                "terminal_has_json_fence": "\x60\x60\x60" in response}
    _json(recovered, obj)
    return {"trial": case_id, "recovery": "RECOVERED_FROM_TERMINAL_JSON",
            "verdict": obj["verdict"], "audio_analyzed_reported": obj["audio_analyzed"],
            "segment_count": len(obj["segments"])}


def recover(private_root: Path, session: str) -> dict:
    priv, manifest = _load_session(private_root, session)
    cases = [recover_case(priv, manifest, item) for item in manifest["cases"]]
    return {"session": session, "source": "EXISTING_PRIVATE_STREAM_JSON",
            "model_called": False, "cases": cases}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    # ASR may render individual spoken Korean digits as Arabic numerals.
    text = re.sub(r"[0-9]", lambda m: DIGITS[int(m.group(0))], text)
    return "".join(re.findall(r"[가-힣a-z]", text))


def cer(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, letter in enumerate(ref, 1):
        current = [i]
        for j, other in enumerate(hyp, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (letter != other)))
        previous = current
    return previous[-1] / len(ref)


def _read_answer(path: Path) -> tuple[str, str, bool]:
    if not path.exists():
        return "MISSING", "", False
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict) or obj.get("verdict") not in {
                "AUDIO_TRANSCRIBED", "NO_AUDIO_ACCESS", "NO_SPEECH"}:
            raise ValueError("Unknown verdict")
        segs = obj.get("segments")
        if not isinstance(segs, list) or any(not isinstance(x, dict) or
                not isinstance(x.get("text"), str) for x in segs):
            raise ValueError("Invalid segments")
        return obj["verdict"], " ".join(x["text"] for x in segs), (
            obj.get("audio_analyzed") is True)
    except (OSError, UnicodeError, ValueError, TypeError):
        return "INVALID", "", False


def _read_acoustic_answer(path: Path) -> tuple[str | None, int | None]:
    """Revalidate a v3 private answer; never emit its transcription."""
    try:
        value = _parse_terminal_json_response(
            path.read_text(encoding="utf-8-sig"), contract=ACOUSTIC_CONTRACT)
    except (OSError, UnicodeError):
        return None, None
    if value is None:
        return None, None
    return value["acoustic_class"], value["beep_count"]


def evaluate(private_root: Path, session: str, threshold: float = 0.25) -> dict:
    if not 0 <= threshold <= 1:
        raise ValueError("CER threshold must be between 0 and 1")
    priv, manifest = _load_session(private_root, session)
    cases = []
    for item in manifest["cases"]:
        workspace = Path(manifest["runtime_root"]) / item["id"]
        media = workspace / "clip_00_30.mp3"
        matched = media.exists() and _sha(media) == item["sha256"]
        original = workspace / "answer.json"
        recovered = priv / "recoveries" / (item["id"] + ".response.json")
        source = "workspace_answer" if original.exists() else (
            "private_terminal_recovery" if recovered.exists() else "missing")
        verdict, text, analyzed = _read_answer(
            original if original.exists() else recovered)
        status_path = priv / (item["id"] + ".status.json")
        output_contract = "legacy_workspace_answer"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            output_contract = status.get("output_contract", "legacy_workspace_answer")
            completed = (status.get("exit_code") == 0 and
                         status.get("submitted") is True and not status.get("timed_out"))
        except (OSError, ValueError):
            completed = False
        provenance = inspect_headless_trace(priv / (item["id"] + ".ndjson"))
        condition = item["kind"]
        acoustic_class = None
        reported_beeps = None
        acoustic_class_match = None
        tone_count_match = None
        if output_contract == ACOUSTIC_CONTRACT:
            acoustic_class, reported_beeps = _read_acoustic_answer(
                original if original.exists() else recovered)
            acoustic_class_match = acoustic_class == condition.upper()
            if condition == "tone":
                tone_count_match = (type(reported_beeps) is int and
                                    reported_beeps == item.get("beep_count"))
        error = cer(item["reference"], text) if condition == "speech" else None
        nonce = normalize(item["nonce"]) in normalize(text) if condition == "speech" else None
        host_output_isolation = (output_contract not in {
            "host_terminal_json_v2", ACOUSTIC_CONTRACT}
            or source == "private_terminal_recovery")
        ok = bool(matched and completed and analyzed and host_output_isolation
                  and (output_contract != ACOUSTIC_CONTRACT or
                       acoustic_class_match and
                       (condition != "tone" or tone_count_match))
                  and provenance["mp3_read_call_observed"]
                  and provenance["terminal_success_observed"]
                  and not provenance["other_tool_calls_observed"] and (
            condition == "speech" and verdict == "AUDIO_TRANSCRIBED"
            and bool(text.strip()) and error <= threshold and nonce
            or condition in {"silence", "tone"} and verdict == "NO_SPEECH"
            and not text.strip()))
        cases.append({"trial": item["id"], "kind": condition,
                      "verdict": verdict, "cer": round(error, 4) if error is not None else None,
                      "nonce_recognized": nonce, "audio_analyzed_reported": analyzed,
                      "audio_hash_matched": matched, "cli_success": completed,
                      "answer_source": source,
                      "output_contract": output_contract,
                      "acoustic_class_reported": acoustic_class,
                      "acoustic_class_match": acoustic_class_match,
                      "tone_count_match": tone_count_match,
                      "host_output_isolation": host_output_isolation,
                      "mp3_read_call_observed": provenance["mp3_read_call_observed"],
                      "other_tool_calls_observed": provenance["other_tool_calls_observed"],
                      "terminal_success_observed": provenance["terminal_success_observed"],
                      "pass": ok})
    speech = [x for x in cases if x["kind"] == "speech"]
    silence = [x for x in cases if x["kind"] == "silence"]
    tones = [x for x in cases if x["kind"] == "tone"]
    all_complete = all(x["cli_success"] and x["verdict"] not in {"MISSING", "INVALID"}
                       for x in cases)
    sufficient = (len(speech) >= 3 and len(silence) >= 1 and len(tones) >= 1
                  if manifest["schema"] == "agy_blind_audio_v2" else
                  len(speech) >= 3 and len(silence) >= 2)
    overall = ("INCOMPLETE" if not all_complete else
               "FAIL_AUDIO_DISCRIMINATION" if not all(x["pass"] for x in cases)
               else "PROVISIONAL_INSUFFICIENT_TRIALS" if not sufficient else
               "PASS_BEHAVIORAL_AUDIO_DISCRIMINATION")
    report = {"schema": "agy_blind_audio_evaluation_v1", "session": session,
              "total_trials": len(cases), "speech_trials": len(speech),
              "tone_trials": len(tones), "silence_trials": len(silence),
              "all_complete": all_complete,
              "threshold_cer": threshold, "overall": overall, "trials": cases,
              "independent_binary_transport_verified": False,
              "note": "Behavioral blind test; no raw transcript or answer key is printed."}
    report_path = priv / "evaluations"
    report_path.mkdir(exist_ok=True)
    # Multiple evaluations may be useful, but never overwrite earlier evidence.
    output = report_path / ("report-" + uuid.uuid4().hex[:12] + ".json")
    _json(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blinded Korean speech/silence AGY Flash trial")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare", help="Generate hidden truth and MP3 controls; no model calls")
    p.add_argument("--runtime-root", type=Path, default=None)
    p.add_argument("--private-root", type=Path, default=None)
    p.add_argument("--speech-count", type=int, default=3)
    p.add_argument("--silence-count", type=int, default=1)
    p.add_argument("--tone-count", type=int, default=1)
    r = sub.add_parser("run", help="Run a single trial only with --submit")
    r.add_argument("--private-root", type=Path, default=None)
    r.add_argument("--session", required=True)
    r.add_argument("--trial", required=True, help="Trial id or 'all' for sequential run")
    r.add_argument("--submit", action="store_true")
    r.add_argument("--auto-approve", action="store_true",
                   help="Explicitly allow tool actions without asking; sandbox may vary by OS")
    r.add_argument("--timeout", type=int, default=240)
    recover_parser = sub.add_parser(
        "recover", help="Recover strict final JSON from previously saved private headless streams")
    recover_parser.add_argument("--private-root", type=Path, default=None)
    recover_parser.add_argument("--session", required=True)
    diagnose_parser = sub.add_parser(
        "diagnose-response",
        help="Classify archived terminal JSON validation errors with no model calls")
    diagnose_parser.add_argument("--private-root", type=Path, default=None)
    diagnose_parser.add_argument("--session", required=True)
    e = sub.add_parser("evaluate", help="Compute private CER, nonce and silence checks")
    e.add_argument("--private-root", type=Path, default=None)
    e.add_argument("--session", required=True)
    e.add_argument("--max-cer", type=float, default=0.25)
    args = parser.parse_args(argv)
    private_root = args.private_root or _default_root("truth")
    if args.action == "prepare":
        result = prepare(args.runtime_root or _default_root("runtime"),
                         private_root, args.speech_count, args.silence_count,
                         args.tone_count)
    elif args.action == "run":
        if not 30 <= args.timeout <= 900:
            parser.error("timeout must be between 30 and 900 seconds")
        if args.auto_approve and not args.submit:
            parser.error("auto-approve requires explicit --submit")
        if args.trial == "all":
            _, manifest = _load_session(private_root, args.session)
            results = [run_trial(private_root, args.session, item["id"],
                                 args.submit, args.auto_approve, args.timeout)
                       for item in manifest["cases"]]
            result = {"session": args.session, "submitted": args.submit,
                      "model_requests": len(results) if args.submit else 0,
                      "trials": results}
        else:
            result = run_trial(private_root, args.session, args.trial,
                               args.submit, args.auto_approve, args.timeout)
    elif args.action == "recover":
        result = recover(private_root, args.session)
    elif args.action == "diagnose-response":
        result = diagnose_terminal_responses(private_root, args.session)
    else:
        result = evaluate(private_root, args.session, args.max_cer)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == "run" and args.submit:
        trials = result.get("trials", [result])
        if any(x.get("exit_code") != 0 or x.get("timed_out")
               or not (x.get("answer_present") or x.get("answer_recovered"))
               for x in trials):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
