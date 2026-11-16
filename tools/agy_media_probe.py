"""Experimental Windows AGY native MP4/WAV attachment runner.

Source-only, offline-testable. Never claim that a clipboard event proves audio
reached the model. Real media, transcripts and screenshots stay in local runtime.
"""
from __future__ import annotations

import argparse
import ctypes
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

MODEL = "gemini-3.8-flash-medium"
RESULT_NAMES = {"audio": "flash_audio.json", "video": "flash_mp4.json"}
HERE = Path(__file__).resolve().parent


def validate_result(path: Path, media_type: str, transport: str = "clipboard") -> dict:
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict) or result.get("verdict") != "AUDIO_TRANSCRIBED":
        raise ValueError("No validated audio transcription; verdict=" + str(
            result.get("verdict") if isinstance(result, dict) else "INVALID"))
    info = result.get("input", {})
    if transport == "file-read":
        if media_type != "audio" or info.get("input_method") != "tool_read_file" \
                or info.get("read_file_used") is not True \
                or info.get("audio_analyzed") is not True:
            raise ValueError("Model did not report audio read through read_file tool")
    else:
        expected = "attached_audio_seen" if media_type == "audio" else "attached_video_seen"
        if info.get(expected) is not True or info.get("audio_analyzed") is not True:
            raise ValueError("Model did not report audio access")
        if info.get("input_method") != f"native_multimodal_{media_type}_attachment":
            raise ValueError("Unexpected input method")
    segments = result.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("No speech segments")
    for s in segments:
        if not isinstance(s, dict) or not str(s.get("text", "")).strip():
            raise ValueError("Empty/invalid segment")
        if not 0 <= float(s.get("start", -1)) < float(s.get("end", -1)) <= 30.25:
            raise ValueError("Invalid segment timestamps")
    return result


def prepare_wav(mp4: Path, wav: Path) -> dict:
    if not mp4.is_file():
        raise FileNotFoundError(mp4)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise FileNotFoundError("ffmpeg and ffprobe must be on PATH")
    wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = wav.with_suffix(".pending.wav")
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-y", "-i", str(mp4), "-map", "0:a:0", "-vn", "-t", "30",
                        "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", str(tmp)],
                       check=True, capture_output=True, timeout=90)
        probed = subprocess.run(["ffprobe", "-v", "error", "-of", "json",
                                "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
                                str(tmp)], check=True, capture_output=True,
                               text=True, timeout=15)
        spec = json.loads(probed.stdout)
        streams = spec["streams"]
        duration = float(spec["format"]["duration"])
        if len(streams) != 1 or streams[0]["codec_name"] != "pcm_s16le" \
                or int(streams[0]["sample_rate"]) != 16000 \
                or int(streams[0]["channels"]) != 1 \
                or not 29 <= duration <= 30.25:
            raise ValueError("Invalid 30-second WAV extraction")
        tmp.replace(wav)
        return {"path": str(wav), "duration": duration, "sha256": sha256(wav)}
    finally:
        tmp.unlink(missing_ok=True)



def prepare_mp3(mp4: Path, mp3: Path) -> dict:
    """Produce a small 30-second mono MP3 for a separate clipboard capability probe."""
    if not mp4.is_file():
        raise FileNotFoundError(mp4)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise FileNotFoundError("ffmpeg and ffprobe must be on PATH")
    mp3.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp3.with_suffix(".pending.mp3")
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-y", "-i", str(mp4), "-map", "0:a:0", "-vn", "-t", "30",
                        "-c:a", "libmp3lame", "-b:a", "96k",
                        "-ar", "16000", "-ac", "1", str(tmp)],
                       check=True, capture_output=True, timeout=90)
        probed = subprocess.run(["ffprobe", "-v", "error", "-of", "json",
                                "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
                                str(tmp)], check=True, capture_output=True,
                               text=True, timeout=15)
        spec = json.loads(probed.stdout)
        streams = spec["streams"]
        duration = float(spec["format"]["duration"])
        if len(streams) != 1 or streams[0]["codec_name"] != "mp3" \
                or int(streams[0]["sample_rate"]) != 16000 \
                or int(streams[0]["channels"]) != 1 \
                or not 29 <= duration <= 30.5:
            raise ValueError("Invalid 30-second MP3 extraction")
        tmp.replace(mp3)
        return {"path": str(mp3), "duration": duration, "codec": "mp3", "sha256": sha256(mp3)}
    finally:
        tmp.unlink(missing_ok=True)


def audio_result_filename(media_type: str, audio_format: str,
                          transport: str = "clipboard") -> str:
    if media_type == "video":
        return "flash_mp4.json"
    if transport == "file-read":
        return "flash_audio_file_read_mp3.json" if audio_format == "mp3" else "flash_audio_file_read_wav.json"
    return "flash_audio_mp3.json" if audio_format == "mp3" else "flash_audio.json"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_prompt(media_type: str, audio_format: str = "wav",
                transport: str = "clipboard") -> str:
    if transport == "file-read":
        if media_type != "audio":
            raise ValueError("file-read mode currently supports audio only")
        filename = "clip_00_30." + audio_format
        output = audio_result_filename(media_type, audio_format, transport)
        prompt = (
            f"작업 디렉터리의 {filename} 오디오 파일을 실제로 분석해 한국어 발화를 "
            "0초부터 30초까지 타임스탬프 포함하여 정확히 전사하세요. "
            "반드시 에이전트의 read_file 도구를 사용해 해당 오디오 파일을 직접 읽어 보세요. "
            "단순히 파일명을 확인하거나 경로만 받은 것은 음성을 들은 것이 아닙니다. "
            "URL, 브라우저, 유튜브 자막, 다른 파일, 로컬 음성인식 프로그램 또는 "
            "별도 명령 실행으로 발화를 추정하지 마세요. 도구가 음성 데이터 대신 "
            "텍스트나 메타데이터만 반환하면 NO_AUDIO_ACCESS로 보고하고 문장을 만들어내지 마세요. "
            f"현재 폴더에 {output} 파일을 UTF-8 JSON으로 작성하세요. "
            '스키마: {"input":{"read_file_used":true,"audio_analyzed":true,'
            '"input_method":"tool_read_file","limitations":[]},'
            '"segments":[{"start":0.0,"end":2.0,"text":"실제 들은 발화"}],'
            '"uncertainties":[],"verdict":"AUDIO_TRANSCRIBED"}. '
            "이 문장은 형식 예시이며 실제 발화가 아닙니다. "
            '접근 실패면 "audio_analyzed":false, "segments":[], '
            '"verdict":"NO_AUDIO_ACCESS"를 쓰세요. '
            "출력 본문에도 JSON을 표시하고, 사용한 read_file 도구 결과의 "
            "종류를 요약하세요."
        )
        return re.sub(r"\s+", " ", prompt).strip()
    kind = ("MP3 오디오" if audio_format == "mp3" else "WAV 오디오") if media_type == "audio" else "MP4 동영상"
    seen = "attached_audio_seen" if media_type == "audio" else "attached_video_seen"
    result_file = audio_result_filename(media_type, audio_format)
    text = (
        f"이 요청에는 실제 {kind} 파일이 네이티브 첨부돼 있어야 합니다. "
        "오직 첨부 파일의 실제 오디오를 듣고 00:00부터 00:30까지 한국어 발화를 "
        "타임스탬프와 함께 충실히 전사하세요. URL, 자막, 브라우저, 웹 검색, "
        "다른 로컬 파일이나 화면의 문자를 발화 근거로 사용하지 마세요. "
        "추측, 교정, 환각을 피하세요. 실제 오디오를 수신할 수 없으면 "
        "NO_AUDIO_ACCESS를 반환하고 임의의 문장을 생성하지 마세요. "
        f"작업 폴더에 {result_file} 파일을 UTF-8 JSON으로 작성하고 존재를 확인하세요. "
        f'필드: {{"input":{{"{seen}":true,"audio_analyzed":true,'
        f'"input_method":"native_multimodal_{media_type}_attachment",'
        '"limitations":[]},"segments":[{"start":0.0,"end":2.0,'
        '"text":"실제로 들린 발화"}],"uncertainties":[],'
        '"verdict":"AUDIO_TRANSCRIBED"}. '
        "위 텍스트는 정답이 아닌 스키마 예시입니다. 오디오 접근 불가시 "
        'input.audio_analyzed=false, segments=[], verdict="NO_AUDIO_ACCESS"로 작성하세요. '
        "응답 본문에도 동일 JSON을 보여주세요."
    )
    return re.sub(r"\s+", " ", text).strip()


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def launcher(workspace: Path, title: str, agy: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"AGY_MEDIA_[0-9A-F]{10}", title):
        raise ValueError("Unsafe window marker")
    ps = workspace / (title + ".ps1")
    state = workspace / (title + ".state")
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Set-Location -LiteralPath " + ps_quote(str(workspace)),
        "$stateFile = " + ps_quote(str(state)),
        "$agyExe = " + ps_quote(agy),
        "try {",
        " if (-not (Test-Path -LiteralPath $agyExe -PathType Leaf)) { throw 'AGY_NOT_FOUND' }",
        " Set-Content -LiteralPath $stateFile -Value 'LAUNCHER_READY' -Encoding UTF8",
        " Write-Host 'AGY_LAUNCHER_READY'",
        " & $agyExe --model " + MODEL +
        " --effort medium --dangerously-skip-permissions --sandbox",
        " Set-Content -LiteralPath $stateFile -Value ('AGY_EXITED:' + $LASTEXITCODE) -Encoding UTF8",
        "} catch {",
        " Set-Content -LiteralPath $stateFile -Value ('LAUNCH_ERROR:' + $_.Exception.Message) -Encoding UTF8",
        "}",
    ]
    ps.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
    return ps, state


def windows(pg) -> list:
    return [(int(w._hWnd), str(w.title or ""), w) for w in pg.getAllWindows()
            if getattr(w, "visible", False)]


def choose_window(before: set[int], after: list, token: str) -> int | None:
    created = [(h, name) for h, name, _ in after if h not in before]
    by_title = [h for h, title in created if token in title]
    return by_title[0] if len(by_title) == 1 else None


def focus(pg, hwnd: int):
    matches = [w for h, _, w in windows(pg) if h == hwnd]
    if len(matches) != 1:
        raise RuntimeError("Target terminal window disappeared")
    w = matches[0]
    if w.isMinimized:
        w.restore()
    w.activate()
    time.sleep(.6)
    active = pg.getActiveWindow()
    if not active or int(active._hWnd) != hwnd:
        raise RuntimeError("Wrong active window; refusing to type")


def wait_for_marker(marker: Path, timeout: int):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if marker.exists():
            value = marker.read_text(encoding="utf-8-sig").strip()
            if value == "LAUNCHER_READY":
                return
            if value.startswith(("LAUNCH_ERROR:", "AGY_EXITED:")):
                raise RuntimeError(value)
        time.sleep(.3)
    raise RuntimeError("Launcher did not report readiness")


def screenshot(pg, workspace: Path, name: str) -> str:
    path = workspace / (name + ".png")
    pg.screenshot().save(str(path))
    return str(path)


def run_gui(workspace: Path, media: Path, kind: str, args) -> dict:
    if os.name != "nt":
        raise RuntimeError("Interactive Windows desktop required")
    import pyautogui as pg
    import pyperclip
    if not shutil.which("wt.exe"):
        raise FileNotFoundError("Windows Terminal wt.exe missing")
    agy = shutil.which("agy") or shutil.which("agy.cmd")
    if not agy:
        raise FileNotFoundError("agy executable not in PATH")
    title = "AGY_MEDIA_" + uuid.uuid4().hex[:10].upper()
    script, marker = launcher(workspace, title, agy)
    before = {h for h, _, _ in windows(pg)}
    pg.FAILSAFE = True
    cmd = ["wt.exe", "-w", "new", "new-tab", "--title", title,
           "--suppressApplicationTitle", "--startingDirectory", str(workspace),
           "powershell.exe", "-NoLogo", "-NoProfile", "-NoExit",
           "-ExecutionPolicy", "RemoteSigned", "-File", str(script)]
    subprocess.Popen(cmd, cwd=workspace)
    end = time.monotonic() + args.startup_seconds
    hwnd = None
    while time.monotonic() < end:
        hwnd = choose_window(before, windows(pg), title)
        if hwnd is not None:
            break
        time.sleep(.5)
    if hwnd is None:
        raise RuntimeError("Cannot uniquely identify a newly created AGY window")
    wait_for_marker(marker, min(args.startup_seconds, 30))
    time.sleep(args.ready_delay_seconds)
    focus(pg, hwnd)
    before_pic = screenshot(pg, workspace, "gui_01_ready")
    pyperclip.copy(safe_prompt(kind, args.audio_format, args.transport))
    pg.hotkey("ctrl", "v", interval=.1)
    time.sleep(2)
    focus(pg, hwnd)
    prompt_pic = screenshot(pg, workspace, "gui_02_prompt")
    if args.transport == "file-read":
        focus(pg, hwnd)
        stage = screenshot(pg, workspace, "gui_03_file_read_prompt")
        evidence = {"launcher": str(marker), "media": str(media),
                    "media_sha256": sha256(media), "transport": "file-read",
                    "window_title": title, "screenshots": [before_pic, prompt_pic, stage],
                    "clipboard_file_drop_ready": False, "native_payload_confirmed": False,
                    "tool_read_observed_independently": False,
                    "note": "File path prompt is not a native attachment; read_file audio support unverified"}
        if args.attachment_probe:
            evidence["status"] = "FILE_READ_PROMPT_STAGED_NO_SUBMISSION"
            return evidence
        focus(pg, hwnd)
        pg.press("enter")
        time.sleep(3)
        evidence["screenshots"].append(screenshot(pg, workspace, "gui_04_submitted"))
        evidence["status"] = "FILE_READ_PROMPT_SUBMITTED_UNVERIFIED"
        return evidence
    helper = HERE / "agy_media_file_drop.ps1"
    proc = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-STA",
                           "-ExecutionPolicy", "Bypass", "-File", str(helper),
                           "-FilePath", str(media)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=25)
    if proc.returncode != 0 or "FILE_DROP_READY" not in proc.stdout:
        raise RuntimeError("File-drop clipboard setup failed: " + proc.stderr[-500:])
    focus(pg, hwnd)
    pg.hotkey(*args.paste_hotkey.split("+"), interval=.1)
    time.sleep(3)
    focus(pg, hwnd)
    attach_pic = screenshot(pg, workspace, "gui_03_attachment")
    evidence = {"launcher": str(marker), "media": str(media),
                "media_sha256": sha256(media),
                "window_title": title, "screenshots": [before_pic, prompt_pic, attach_pic],
                "clipboard_file_drop_ready": True,
                "media_format": media.suffix.lstrip("."),
                "native_payload_confirmed": False,
                "note": "UI screenshot and model self-report are not independent transport proof"}
    if args.attachment_probe:
        evidence["status"] = "STAGED_NO_SUBMISSION"
        return evidence
    # Explicit --submit prevents accidentally issuing billable model calls.
    focus(pg, hwnd)
    pg.press("enter")
    time.sleep(3)
    evidence["screenshots"].append(screenshot(pg, workspace, "gui_04_submitted"))
    evidence["status"] = "SUBMITTED"
    return evidence


def wait_result(path: Path, media_type: str, timeout: int,
                transport: str = "clipboard"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            try:
                obj = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(obj, dict) and obj.get("verdict") in {
                    "NO_AUDIO_ACCESS", "NO_AUDIO_ATTACHED", "UNSUPPORTED_AUDIO_FORMAT"
                }:
                    raise RuntimeError("AGY reported " + obj["verdict"])
                return validate_result(path, media_type, transport)
            except (ValueError, json.JSONDecodeError):
                pass
        time.sleep(2)
    raise TimeoutError("No validated Flash audio transcript before timeout")


def normalized(text: str) -> str:
    return "".join(re.findall(r"[가-힣a-z0-9]", text.lower()))


def compare_references(run: Path, model_result: dict, media_type: str,
                       transport: str = "clipboard", audio_format: str = "wav"):
    refs = run / "references"
    a = refs / "auto_ko_00_30.json"
    b = refs / "manual_ko_00_30.json"
    if not a.is_file() or not b.is_file():
        return None

    def read_reference(path):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return " ".join(str(e.get("text", "")) for e in data.get("events", [])
                        if isinstance(e, dict) and float(e.get("start", 99)) < 30)
    texts = {
        "auto": read_reference(a),
        "manual": read_reference(b),
        "flash": " ".join(s["text"] for s in model_result["segments"]),
    }
    ratios = {
        "auto_manual": SequenceMatcher(None, normalized(texts["auto"]),
                                       normalized(texts["manual"]), autojunk=False).ratio(),
        "flash_auto": SequenceMatcher(None, normalized(texts["flash"]),
                                     normalized(texts["auto"]), autojunk=False).ratio(),
        "flash_manual": SequenceMatcher(None, normalized(texts["flash"]),
                                       normalized(texts["manual"]), autojunk=False).ratio(),
    }
    suffix = (f"_audio_file_read_{audio_format}" if transport == "file-read" else
              "_audio" if media_type == "audio" else "")
    report = {"text_similarity_not_accuracy": ratios,
              "media_type": media_type, "human_audio_ground_truth_verified": False}
    (run / f"comparison{suffix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Experimental AGY native MP4/WAV attachment")
    parser.add_argument("--run", type=Path, required=True, help="Local runtime directory")
    parser.add_argument("--media-type", choices=["audio", "video"], default="audio")
    parser.add_argument("--transport", choices=["clipboard", "file-read"],
                        default="clipboard",
                        help="Clipboard accepts image/video only; file-read is experimental audio tool input")
    parser.add_argument("--audio-format", choices=["wav", "mp3"], default="wav",
                        help="Choose WAV or MP3 clipboard attachment probe")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--attachment-probe", action="store_true",
                        help="Stage media; do not submit or call the model")
    parser.add_argument("--submit", action="store_true", help="Send AGY model request")
    parser.add_argument("--ready-delay-seconds", type=float, default=12)
    parser.add_argument("--startup-seconds", type=int, default=90)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--paste-hotkey", choices=["alt+v", "ctrl+v"], default="alt+v")
    args = parser.parse_args(argv)
    if args.submit == args.attachment_probe and not args.dry_run:
        parser.error("Choose exactly one of --attachment-probe or --submit")
    if args.transport == "file-read" and args.media_type != "audio":
        parser.error("file-read transport is only supported for audio experiments")
    if args.media_type == "audio" and args.transport == "clipboard" and args.submit:
        parser.error("AUDIO_CLIPBOARD_UNSUPPORTED: AGY 1.5.9 Alt+V reports 'No image or video found'. "
                     "Use --transport file-read for a separate tool-based experiment; "
                     "file-read is NOT a native media attachment.")
    run = args.run.resolve()
    workspace = run / "agy_clean_workspace"
    mp4 = workspace / "clip_00_30.mp4"
    if not mp4.is_file():
        raise FileNotFoundError(mp4)
    media = (workspace / ("clip_00_30." + args.audio_format)
             if args.media_type == "audio" else mp4)
    result_path = workspace / audio_result_filename(args.media_type, args.audio_format,
                                                     args.transport)
    status_path = run / (
        ("automation_status_audio_file_read_" + args.audio_format + ".json")
        if args.transport == "file-read" else
        ("automation_status_audio_mp3.json" if args.audio_format == "mp3"
         else "automation_status_audio.json") if args.media_type == "audio" else
        "automation_status.json")
    if args.dry_run:
        print("DRY_RUN:", {"run": str(run), "media_type": args.media_type,
                           "model": MODEL, "video_exists": True})
        return 0
    if result_path.exists():
        raise FileExistsError(f"Existing result must be archived, not overwritten: {result_path}")
    if args.media_type == "audio":
        manifest = (prepare_wav(mp4, media) if args.audio_format == "wav"
                    else prepare_mp3(mp4, media))
        (run / ("audio_manifest_mp3.json" if args.audio_format == "mp3"
                else "audio_manifest.json")).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    evidence = {"audio_format": args.audio_format,
                "media_type": args.media_type,
                "attachment_transport_verified": False}
    try:
        evidence = run_gui(workspace, media, args.media_type, args)
        if args.attachment_probe:
            print("STAGED_NO_SUBMISSION:", evidence["screenshots"][-1])
            return 0
        result = wait_result(result_path, args.media_type, args.timeout_seconds,
                             args.transport)
        comparison = compare_references(run, result, args.media_type,
                                        args.transport, args.audio_format)
        evidence["status"] = "MODEL_SELF_REPORTED_AUDIO_TRANSCRIBED_UNVERIFIED"
        evidence["comparison_created"] = comparison is not None
        print("TRANSCRIPTION_SELF_REPORTED:", result_path)
        return 0
    except Exception as exc:
        evidence["status"] = "FAILED_OR_UNVERIFIED"
        evidence["error"] = str(exc)
        print("EXPERIMENT_FAILED:", exc)
        return 2
    finally:
        status_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                               encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
