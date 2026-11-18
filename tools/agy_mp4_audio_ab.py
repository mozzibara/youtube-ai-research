"""Offline MP4(AAC) versus original MP3 read_file audio behavior comparison.

Reuses existing private blinded MP3 controls. Never launches AGY, submits
prompts, writes into the original session, or copies answer keys into media
workspaces. All sources and scored outputs remain local and untracked.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import uuid

from tools import agy_blind_audio as blind
from tools.agy_audio_transport_audit import _read_manifest, _regular_file

SOURCE_RE = re.compile(r"blind-[0-9a-f]{16}\Z")
COMPARE_RE = re.compile(r"compare-[0-9a-f]{16}\Z")
CASE_RE = re.compile(r"case-[0-9a-f]{12}\Z")
MAX_MEDIA = 12 * 1024 * 1024
PROMPT = (
    "첨부된 MP4 동영상의 실제 프레임과 실제 오디오를 각각 분석하세요. "
    "화면 중앙에 표시되는 여섯 자리 숫자를 visual_code에 기록하세요. "
    "오디오는 SPEECH, TONE, SILENCE, UNAVAILABLE 중 하나로 구별하세요. "
    "SPEECH라면 들은 한국어 발화를 빠짐없이 전사하고, TONE이면 비프 횟수를 세세요. "
    "실제 소리가 들리지 않는다면 추측하지 말고 SILENCE 또는 UNAVAILABLE로 표시하세요. "
    "화면 숫자로 발화 내용을 추측하거나 다른 파일, 명령 실행, 웹, 자막을 이용하지 마세요. "
    "오직 JSON 객체 하나만 답하세요. 예시 형식: "
    '{"visual_code":"화면의 여섯 자리 숫자 또는 null",'
    '"acoustic_class":"SPEECH|TONE|SILENCE|UNAVAILABLE",'
    '"beep_count":0,"segments":[{"text":"실제로 들은 문장"}]}. '
    "비프나 무음이면 segments=[]이고 접근 불가면 beep_count=null입니다."
)
DIGIT_BITMAP = {
    "0": ("11111","10001","10011","10101","11001","10001","11111"),
    "1": ("00100","01100","00100","00100","00100","00100","01110"),
    "2": ("11111","00001","00001","11111","10000","10000","11111"),
    "3": ("11111","00001","00001","01111","00001","00001","11111"),
    "4": ("10001","10001","10001","11111","00001","00001","00001"),
    "5": ("11111","10000","10000","11111","00001","00001","11111"),
    "6": ("11111","10000","10000","11111","10001","10001","11111"),
    "7": ("11111","00001","00010","00100","01000","01000","01000"),
    "8": ("11111","10001","10001","11111","10001","10001","11111"),
    "9": ("11111","10001","10001","11111","00001","00001","11111"),
}


def _default(name: str) -> Path:
    home = os.environ.get("LOCALAPPDATA")
    if not home:
        raise ValueError("LOCALAPPDATA required or specify root")
    return Path(home) / name


def _separate_roots(*roots: Path) -> list[Path]:
    resolved = [p.resolve() for p in roots]
    repo = Path(__file__).resolve().parents[1]
    for i, path in enumerate(resolved):
        if path == repo or path.is_relative_to(repo):
            raise ValueError("Runtime/truth paths must be outside repository")
        if any(path == other or path.is_relative_to(other) or
               other.is_relative_to(path)
               for j, other in enumerate(resolved) if i != j):
            raise ValueError("All roots must be disjoint")
    return resolved


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _frame(ppm: Path, code: str) -> None:
    """Render visual-only six-digit decoy with stdlib, no font dependencies."""
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("Invalid visual code")
    width, height, scale = 640, 360, 12
    rgb = bytearray(bytes((13, 25, 45)) * (width * height))
    pitch = 6 * scale
    x0 = (width - (6 * 5 * scale + 5 * scale)) // 2
    y0 = (height - 7 * scale) // 2
    for i, char in enumerate(code):
        for cy, line in enumerate(DIGIT_BITMAP[char]):
            for cx, on in enumerate(line):
                if on != "1":
                    continue
                xx = x0 + i * pitch + cx * scale
                yy = y0 + cy * scale
                for y in range(yy, yy + scale):
                    start = 3 * (width * y + xx)
                    rgb[start:start + 3 * scale] = bytes((232, 241, 255)) * scale
    with ppm.open("xb") as dest:
        dest.write(f"P6\n{width} {height}\n255\n".encode("ascii"))
        dest.write(rgb)


def _ffmpeg_binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ValueError("FFmpeg and FFprobe required")
    return found


def _probe_media(path: Path) -> dict:
    exe = _ffmpeg_binary("ffprobe")
    result = subprocess.run(
        [exe, "-v", "error", "-of", "json",
         "-show_entries", "stream=codec_type,codec_name,sample_rate:format=duration",
         str(path)], stdin=subprocess.DEVNULL, capture_output=True,
        text=True, check=True, timeout=20)
    raw = json.loads(result.stdout)
    return {"duration": float(raw["format"]["duration"]),
            "streams": raw["streams"]}


def _audio_profile(path: Path) -> tuple[list[float], float]:
    ffmpeg = _ffmpeg_binary("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
         "-map", "0:a:0", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", "-f", "s16le", "-"],
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=45)
    raw = proc.stdout
    if len(raw) > 4_000_000 or len(raw) < 16000:
        raise ValueError("Unexpected decoded audio size")
    samples = array("h")
    samples.frombytes(raw[:len(raw) - len(raw) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    bucket = 1600
    energies = []
    for pos in range(0, len(samples) - bucket + 1, bucket):
        energies.append(math.sqrt(sum(x*x for x in samples[pos:pos+bucket]) / bucket))
    rms = math.sqrt(sum(x*x for x in samples) / max(1, len(samples)))
    return energies, rms


def _profile_matches(original: Path, remuxed: Path) -> bool:
    old, old_rms = _audio_profile(original)
    new, new_rms = _audio_profile(remuxed)
    if abs(len(old) - len(new)) > 3:
        return False
    if max(old_rms, new_rms) < 15:
        return True
    if min(old_rms, new_rms) < 15 or not .5 <= new_rms / old_rms <= 2:
        return False
    # Lossy MP3 -> AAC transcoding cannot match encoded-byte hashes.
    # Compare short-window energy envelopes with up to 0.3s alignment.
    scores = []
    for offset in range(-3, 4):
        paired = [(old[i], new[i + offset]) for i in range(len(old))
                  if 0 <= i + offset < len(new)]
        if not paired:
            continue
        numerator = sum(a*b for a,b in paired)
        denom = math.sqrt(sum(a*a for a,b in paired) *
                          sum(b*b for a,b in paired))
        if denom:
            scores.append(numerator / denom)
    return bool(scores and max(scores) >= .93)


def _old_mp3_behavior(old_priv: Path, source: dict, item: dict) -> bool | None:
    workspace = Path(source["runtime_root"]) / item["id"]
    original = workspace / "answer.json"
    recovery = old_priv / "recoveries" / (item["id"] + ".response.json")
    response = original if original.is_file() else recovery
    if not response.is_file() or response.is_symlink():
        return None
    verdict, text, analyzed = blind._read_answer(response)
    kind = item["kind"]
    if kind == "speech":
        return bool(analyzed and verdict == "AUDIO_TRANSCRIBED" and
                    blind.cer(item["reference"], text) <= .25 and
                    blind.normalize(item["nonce"]) in blind.normalize(text))
    if kind not in {"tone", "silence"}:
        return None
    acoustic, beeps = blind._read_acoustic_answer(response)
    return bool(analyzed and verdict == "NO_SPEECH" and not text.strip()
                and acoustic == kind.upper() and
                (kind == "silence" and beeps == 0 or
                 kind == "tone" and beeps == item["beep_count"]))


def prepare(source_session: str, source_truth: Path, runtime: Path,
            truth: Path) -> dict:
    source_truth, runtime, truth = _separate_roots(
        source_truth, runtime, truth)
    if not SOURCE_RE.fullmatch(source_session):
        raise ValueError("Invalid source session ID")
    source_priv, source = _read_manifest(source_truth, source_session)
    _ffmpeg_binary("ffmpeg")
    _ffmpeg_binary("ffprobe")
    identifier = "compare-" + uuid.uuid4().hex[:16]
    pub, priv = runtime / identifier, truth / identifier
    pub.mkdir(parents=True, exist_ok=False)
    priv.mkdir(parents=True, exist_ok=False)
    items = []
    for item in source["cases"]:
        case = item["id"]
        source_mp3 = Path(source["runtime_root"]) / case / "clip_00_30.mp3"
        if (source_mp3.is_symlink() or
                not _regular_file(source_mp3, MAX_MEDIA) or
                _sha(source_mp3) != item["sha256"]):
            raise ValueError("Source audio missing, unsafe or hash mismatch")
        workspace = pub / case
        workspace.mkdir()
        visual = "".join(secrets.choice("0123456789") for _ in range(6))
        while (item.get("nonce") and
               blind.normalize(visual) == blind.normalize(item["nonce"])):
            visual = "".join(secrets.choice("0123456789") for _ in range(6))
        ppm = priv / ("visual-" + secrets.token_hex(8) + ".ppm")
        mp4 = workspace / "clip_00_30.mp4"
        try:
            _frame(ppm, visual)
            ffmpeg = _ffmpeg_binary("ffmpeg")
            subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
                 "-loop", "1", "-framerate", "1", "-i", str(ppm),
                 "-i", str(source_mp3), "-map", "0:v:0", "-map", "1:a:0",
                 "-t", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-r", "1", "-c:a", "aac", "-b:a", "96k", "-ar", "16000",
                 "-ac", "1", "-movflags", "+faststart", str(mp4)],
                stdin=subprocess.DEVNULL, capture_output=True, check=True,
                timeout=120)
        finally:
            ppm.unlink(missing_ok=True)
        meta = _probe_media(mp4)
        streams = meta["streams"]
        if not 29.5 <= meta["duration"] <= 30.5 or not (
            any(s.get("codec_type") == "video" and
                s.get("codec_name") == "h264" for s in streams)
            and any(s.get("codec_type") == "audio" and
                    s.get("codec_name") == "aac" for s in streams)
        ):
            raise ValueError("MP4 video/AAC stream validation failed")
        if not _profile_matches(source_mp3, mp4):
            raise ValueError("MP4 decoded soundtrack differs from source MP3")
        items.append({"id": case, "source_mp3_sha256": item["sha256"],
                      "mp4_sha256": _sha(mp4), "visual_code": visual,
                      "soundtrack_profile_verified": True})
    record = {"schema": "agy_mp4_mp3_comparison_v1",
              "session": identifier, "source_session": source_session,
              "source_truth_root": str(source_truth),
              "runtime_root": str(pub), "cases": items}
    (priv / "manifest.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"schema": "agy_mp4_mp3_comparison_prepared_v1",
            "session": identifier, "case_count": len(items),
            "runtime_root": str(pub), "private_root": str(priv),
            "trials": [{"trial": x["id"], "mp4_file": str(
                pub / x["id"] / "clip_00_30.mp4"),
                "aac_soundtrack_profile_verified": True} for x in items],
            "model_called": False, "private_evidence_modified": False,
            "provider_audio_ingress_verified": False}


def _load(truth: Path, session: str) -> tuple[Path, dict]:
    if not COMPARE_RE.fullmatch(session):
        raise ValueError("Invalid compare session")
    priv = truth.resolve() / session
    manifest = priv / "manifest.json"
    if priv.is_symlink() or not _regular_file(manifest, 100_000):
        raise ValueError("Unsafe or missing comparison manifest")
    obj = json.loads(manifest.read_text(encoding="utf-8"))
    if obj.get("schema") != "agy_mp4_mp3_comparison_v1" or obj.get("session") != session:
        raise ValueError("Unexpected comparison manifest")
    return priv, obj


def _parse_answer(content: str) -> dict | None:
    if not isinstance(content, str) or len(content) > 100_000:
        return None
    data = content.strip()
    if data.startswith("```json") and data.endswith("```"):
        data = data[7:-3].strip()
    elif data.startswith("```") and data.endswith("```"):
        data = data[3:-3].strip()
    try:
        value = json.loads(data)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    if (not isinstance(value.get("visual_code"), (str, type(None)))
            or value.get("acoustic_class") not in
            {"SPEECH", "TONE", "SILENCE", "UNAVAILABLE"}
            or not isinstance(value.get("segments"), list)
            or len(value["segments"]) > 60):
        return None
    if any(not isinstance(s, dict) or
           not isinstance(s.get("text"), str) or len(s["text"]) > 2000
           for s in value["segments"]):
        return None
    return value


def import_response(truth: Path, session: str, trial: str,
                    response: Path) -> dict:
    priv, manifest = _load(truth, session)
    if not CASE_RE.fullmatch(trial) or not any(
            item["id"] == trial for item in manifest["cases"]):
        raise ValueError("Unknown case")
    if not _regular_file(response, 100_000) or response.is_symlink():
        raise ValueError("Unsafe model response file")
    # The user saves this response manually, outside the AGY workspace.
    # This command never invokes AGY or overwrites existing evidence.
    if response.resolve().is_relative_to(Path(manifest["runtime_root"]).resolve()):
        raise ValueError("Response must not originate in model workspace")
    data = response.read_text(encoding="utf-8-sig")
    if _parse_answer(data) is None:
        raise ValueError("Response is not valid experiment JSON")
    target_dir = priv / "responses"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / (trial + ".json")
    with target.open("x", encoding="utf-8") as stream:
        stream.write(data)
    return {"session": session, "trial": trial, "recorded": True,
            "model_called": False, "raw_answer_disclosed": False}


def score(truth: Path, session: str) -> dict:
    priv, manifest = _load(truth, session)
    old_priv, old = _read_manifest(
        Path(manifest["source_truth_root"]), manifest["source_session"])
    originals = {item["id"]: item for item in old["cases"]}
    cases = []
    for item in manifest["cases"]:
        case = item["id"]
        src = originals[case]
        mp4 = Path(manifest["runtime_root"]) / case / "clip_00_30.mp4"
        intact = bool(_regular_file(mp4, MAX_MEDIA) and
                      _sha(mp4) == item["mp4_sha256"])
        response = priv / "responses" / (case + ".json")
        answer = (_parse_answer(response.read_text(encoding="utf-8-sig"))
                  if _regular_file(response, 100_000) else None)
        visual_ok = None
        audio_ok = None
        if answer is not None and intact:
            visual_ok = answer.get("visual_code") == item["visual_code"]
            speech = " ".join(s["text"] for s in answer["segments"])
            cl = answer["acoustic_class"]
            kind = src["kind"]
            if kind == "speech":
                audio_ok = bool(cl == "SPEECH" and
                    blind.cer(src["reference"], speech) <= .25 and
                    blind.normalize(src["nonce"]) in blind.normalize(speech))
            elif kind == "silence":
                audio_ok = cl == "SILENCE" and not speech.strip()
            elif kind == "tone":
                audio_ok = (cl == "TONE" and not speech.strip()
                            and type(answer.get("beep_count")) is int
                            and answer["beep_count"] == src["beep_count"])
        old_ok = _old_mp3_behavior(old_priv, old, src)
        if audio_ok is None or old_ok is None:
            comparison = "INCOMPLETE"
        elif audio_ok and old_ok:
            comparison = "BOTH_BEHAVIORAL_MATCH"
        elif audio_ok:
            comparison = "MP4_ONLY_BEHAVIORAL_MATCH"
        elif old_ok:
            comparison = "MP3_ONLY_BEHAVIORAL_MATCH"
        else:
            comparison = "NEITHER_BEHAVIORAL_MATCH"
        cases.append({"trial": case, "source_mp3_integrity": bool(
            _regular_file(Path(old["runtime_root"]) / case / "clip_00_30.mp3",
                          MAX_MEDIA) and _sha(
              Path(old["runtime_root"]) / case / "clip_00_30.mp3") ==
              item["source_mp3_sha256"]),
                      "mp4_integrity": intact,
                      "mp4_visual_code_match": visual_ok,
                      "mp4_audio_behavior_match": audio_ok,
                      "prior_mp3_audio_behavior_match": old_ok,
                      "comparison": comparison,
                      "provider_audio_ingress_verified": False})
    return {"schema": "agy_mp4_mp3_comparison_score_v1",
            "session": session, "trials": cases,
            "model_called": False, "private_evidence_modified": False,
            "independent_binary_transport_verified": False,
            "note": ("Matched soundtracks and blinded behavior are not provider "
                     "request receipts; codec transcode MP3-to-AAC is lossy.")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline MP4/AAC vs archived MP3 audio controls; never calls AGY")
    subs = parser.add_subparsers(dest="action", required=True)
    p = subs.add_parser("prepare")
    p.add_argument("--source-session", required=True)
    p.add_argument("--source-truth", type=Path)
    p.add_argument("--runtime", type=Path)
    p.add_argument("--truth", type=Path)
    i = subs.add_parser("import-response")
    i.add_argument("--session", required=True)
    i.add_argument("--trial", required=True)
    i.add_argument("--file", required=True, type=Path)
    i.add_argument("--truth", type=Path)
    s = subs.add_parser("score")
    s.add_argument("--session", required=True)
    s.add_argument("--truth", type=Path)
    subs.add_parser("prompt")
    args = parser.parse_args(argv)
    if args.action == "prompt":
        print(PROMPT)
        return 0
    try:
        if args.action == "prepare":
            report = prepare(args.source_session,
                             args.source_truth or _default("AGYBlindAudio/truth"),
                             args.runtime or _default("AGYVideoAB/runtime"),
                             args.truth or _default("AGYVideoAB/truth"))
        elif args.action == "import-response":
            report = import_response(args.truth or _default("AGYVideoAB/truth"),
                                     args.session, args.trial, args.file)
        else:
            report = score(args.truth or _default("AGYVideoAB/truth"), args.session)
    except (OSError, ValueError, subprocess.SubprocessError, KeyError,
            TypeError, ArithmeticError):
        print(json.dumps({"status": "PRIVATE_OFFLINE_COMPARISON_UNAVAILABLE",
                          "model_called": False}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
