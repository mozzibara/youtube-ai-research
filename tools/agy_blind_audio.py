"""Offline-generated blinded Korean speech vs silence evaluation for AGY Flash.

No answer key is placed in an AGY workspace or public Git checkout. This tool
does NOT claim that an AGY read_file tool call independently proves audio access.
External tools (Windows Korean SAPI, ffmpeg, agy) run only on explicit commands.
"""
from __future__ import annotations

import argparse
from array import array
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
    "파일 경로 및 메타데이터만 받았으면 NO_AUDIO_ACCESS로 보고하세요. "
    "외부 파일, 웹 검색, 실행 명령, 음성 인식 프로그램, 참고 자막을 사용하지 마세요. "
    "출력은 현재 폴더에 answer.json UTF-8 JSON 파일로 저장하고 같은 JSON을 응답하세요. "
    '스키마: {"verdict":"AUDIO_TRANSCRIBED|NO_SPEECH|NO_AUDIO_ACCESS",'
    '"audio_analyzed":true,"segments":[{"start":0.0,"end":2.0,"text":"실제 발화"}]}. '
    "말이 들리지 않으면 segments는 빈 배열, verdict는 NO_SPEECH입니다. "
    "오디오 자체에 접근하지 못했다면 audio_analyzed=false, segments=[], "
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


def plan(speech_count: int = 3, silence_count: int = 2) -> list[dict]:
    if not 1 <= speech_count <= 10 or not 1 <= silence_count <= 10:
        raise ValueError("speech/silence count must be between 1 and 10")
    cases = new_phrases(speech_count) + [
        {"kind": "silence", "reference": "", "nonce": ""} for _ in range(silence_count)
    ]
    secrets.SystemRandom().shuffle(cases)
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


def _encode_mp3(out: Path, phrase: str | None, private_dir: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe must be on PATH")
    if out.exists():
        raise FileExistsError(out)
    wav_path = None
    try:
        if phrase is not None:
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
            speech_count: int = 3, silence_count: int = 2) -> dict:
    runtime_root, private_root = _ensure_roots(runtime_root, private_root)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg/ffprobe are required")
    session = "blind-" + uuid.uuid4().hex[:16]
    pub, priv = runtime_root / session, private_root / session
    pub.mkdir(parents=True, exist_ok=False)
    priv.mkdir(parents=True, exist_ok=False)
    records = []
    for item in plan(speech_count, silence_count):
        case_id = "case-" + uuid.uuid4().hex[:12]
        workspace = pub / case_id
        workspace.mkdir(parents=True)
        media = workspace / "clip_00_30.mp3"
        _encode_mp3(media, item["reference"] if item["kind"] == "speech" else None, priv)
        records.append({
            "id": case_id, "kind": item["kind"], "reference": item["reference"],
            "nonce": item["nonce"], "sha256": _sha(media),
        })
    manifest = {"schema": "agy_blind_audio_v1", "session": session,
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
    if manifest.get("schema") != "agy_blind_audio_v1" or manifest.get("session") != session:
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
    if answer.exists() or log.exists():
        raise FileExistsError("Trial already attempted; original evidence must not be overwritten")
    if not submit:
        return {"trial": case_id, "submitted": False, "model": MODEL}
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
                      "timed_out": False, "auto_approve": auto_approve}
        except subprocess.TimeoutExpired:
            status = {"submitted": True, "exit_code": None,
                      "timed_out": True, "auto_approve": auto_approve}
    _json(status_file, status)
    return {"trial": case_id, "submitted": True, "exit_code": status["exit_code"],
            "timed_out": status["timed_out"],
            "answer_present": answer.is_file(),
            "raw_cli_log_private": True}


def inspect_headless_trace(path: Path) -> dict:
    """Extract only provenance flags from actual typed AGY stream-json tool events."""
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
            elif name not in {"write_file", "write_to_file"}:
                forbidden.add(index)
    return {
        "mp3_read_call_observed": bool(reads),
        "other_tool_calls_observed": bool(forbidden),
        "terminal_success_observed": result_success,
    }


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


def evaluate(private_root: Path, session: str, threshold: float = 0.25) -> dict:
    if not 0 <= threshold <= 1:
        raise ValueError("CER threshold must be between 0 and 1")
    priv, manifest = _load_session(private_root, session)
    cases = []
    for item in manifest["cases"]:
        workspace = Path(manifest["runtime_root"]) / item["id"]
        media = workspace / "clip_00_30.mp3"
        matched = media.exists() and _sha(media) == item["sha256"]
        verdict, text, analyzed = _read_answer(workspace / "answer.json")
        status_path = priv / (item["id"] + ".status.json")
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            completed = (status.get("exit_code") == 0 and
                         status.get("submitted") is True and not status.get("timed_out"))
        except (OSError, ValueError):
            completed = False
        provenance = inspect_headless_trace(priv / (item["id"] + ".ndjson"))
        condition = item["kind"]
        error = cer(item["reference"], text) if condition == "speech" else None
        nonce = normalize(item["nonce"]) in normalize(text) if condition == "speech" else None
        ok = bool(matched and completed and analyzed
                  and provenance["mp3_read_call_observed"]
                  and provenance["terminal_success_observed"]
                  and not provenance["other_tool_calls_observed"] and (
            condition == "speech" and verdict == "AUDIO_TRANSCRIBED"
            and bool(text.strip()) and error <= threshold and nonce
            or condition == "silence" and verdict == "NO_SPEECH"
            and not text.strip()))
        cases.append({"trial": item["id"], "kind": condition,
                      "verdict": verdict, "cer": round(error, 4) if error is not None else None,
                      "nonce_recognized": nonce, "audio_analyzed_reported": analyzed,
                      "audio_hash_matched": matched, "cli_success": completed,
                      "mp3_read_call_observed": provenance["mp3_read_call_observed"],
                      "other_tool_calls_observed": provenance["other_tool_calls_observed"],
                      "terminal_success_observed": provenance["terminal_success_observed"],
                      "pass": ok})
    speech = [x for x in cases if x["kind"] == "speech"]
    silence = [x for x in cases if x["kind"] == "silence"]
    all_complete = all(x["cli_success"] and x["verdict"] not in {"MISSING", "INVALID"}
                       for x in cases)
    sufficient = len(speech) >= 3 and len(silence) >= 2
    overall = ("INCOMPLETE" if not all_complete else
               "PROVISIONAL_INSUFFICIENT_TRIALS" if not sufficient else
               "PASS_BEHAVIORAL_AUDIO_DISCRIMINATION" if all(x["pass"] for x in cases)
               else "FAIL_AUDIO_DISCRIMINATION")
    report = {"schema": "agy_blind_audio_evaluation_v1", "session": session,
              "total_trials": len(cases), "speech_trials": len(speech),
              "silence_trials": len(silence), "all_complete": all_complete,
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
    p.add_argument("--silence-count", type=int, default=2)
    r = sub.add_parser("run", help="Run a single trial only with --submit")
    r.add_argument("--private-root", type=Path, default=None)
    r.add_argument("--session", required=True)
    r.add_argument("--trial", required=True, help="Trial id or 'all' for sequential run")
    r.add_argument("--submit", action="store_true")
    r.add_argument("--auto-approve", action="store_true",
                   help="Explicitly allow tool actions without asking; sandbox may vary by OS")
    r.add_argument("--timeout", type=int, default=240)
    e = sub.add_parser("evaluate", help="Compute private CER, nonce and silence checks")
    e.add_argument("--private-root", type=Path, default=None)
    e.add_argument("--session", required=True)
    e.add_argument("--max-cer", type=float, default=0.25)
    args = parser.parse_args(argv)
    private_root = args.private_root or _default_root("truth")
    if args.action == "prepare":
        result = prepare(args.runtime_root or _default_root("runtime"),
                         private_root, args.speech_count, args.silence_count)
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
    else:
        result = evaluate(private_root, args.session, args.max_cer)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == "run" and args.submit:
        trials = result.get("trials", [result])
        if any(x.get("exit_code") != 0 or x.get("timed_out")
               or not x.get("answer_present") for x in trials):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
