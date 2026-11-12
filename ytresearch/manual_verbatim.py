"""Manual full-speech transcription handoff. No provider calls or batching."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import heapq
import json
import os
import shutil
import subprocess
from pathlib import Path
import re

from .research_state import read_source_ledger

ROLES = {"primary_video", "highres_video", "audio", "subtitle_json3"}
VIDEO_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")


class VerbatimError(ValueError):
    pass


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inputs(root, manifest_path):
    root = Path(root).resolve()
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise VerbatimError("MANIFEST_NOT_FOUND")
    if not manifest_path.resolve().is_relative_to(root / "data"):
        raise VerbatimError("MANIFEST_OUTSIDE_RUNTIME")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vid = manifest.get("video_id")
    if not isinstance(vid, str) or not VIDEO_RE.fullmatch(vid):
        raise VerbatimError("VIDEO_ID_INVALID")
    if vid not in {x["video_id"] for x in read_source_ledger(root, expected=77)["videos"]}:
        raise VerbatimError("VIDEO_NOT_REGISTERED")
    assets = manifest.get("assets")
    if not isinstance(assets, dict) or set(assets) != ROLES:
        raise VerbatimError("ASSET_ROLES_INVALID")
    found = {}
    for key in sorted(ROLES):
        item = assets[key]
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise VerbatimError("ASSET_INVALID")
        path = Path(item["path"])
        if path.is_symlink() or not path.is_file():
            raise VerbatimError("ASSET_UNAVAILABLE")
        if type(item.get("size_bytes")) is not int or path.stat().st_size != item["size_bytes"]:
            raise VerbatimError("ASSET_SIZE_MISMATCH")
        h = item.get("sha256")
        if not isinstance(h, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", h):
            raise VerbatimError("ASSET_HASH_INVALID")
        if sha(path) != h.lower():
            raise VerbatimError("ASSET_HASH_MISMATCH")
        found[key] = path.resolve()
    return found, root / "data" / "verbatim" / vid


def create_immutable(path, raw):
    path = Path(path)
    if path.is_symlink():
        raise VerbatimError("OUTPUT_SYMLINK")
    if path.exists():
        if path.is_file() and sha(path) == hashlib.sha256(raw).hexdigest():
            return
        raise VerbatimError("OUTPUT_CONFLICT")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def clock(ms):
    secs, frac = divmod(ms, 1000)
    return f"{secs//3600:02d}:{secs//60%60:02d}:{secs%60:02d}.{frac:03d}"


def render_captions(raw):
    data = json.loads(raw)
    events = data.get("events")
    if not isinstance(events, list):
        raise VerbatimError("JSON3_EVENTS_INVALID")
    lines = [
        "# Full supplied JSON3 caption text; not verified against audio",
        "",
        "Exact utf8 strings of all nonempty caption events, in original order.",
        "No rewriting, dropping filler words or whitespace normalization.",
        "The original JSON3 also preserves non-text events.",
        "",
    ]
    count = 0
    for n, event in enumerate(events, 1):
        if not isinstance(event, dict) or not isinstance(event.get("segs"), list):
            continue
        original = "".join(x["utf8"] for x in event["segs"]
                           if isinstance(x, dict) and isinstance(x.get("utf8"), str))
        if original == "":
            continue
        try:
            start = int(event["tStartMs"])
            length = int(event.get("dDurationMs") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise VerbatimError("JSON3_TIME_INVALID") from exc
        if start < 0 or length < 0:
            raise VerbatimError("JSON3_TIME_NEGATIVE")
        count += 1
        fence = chr(96) * (max([len(x.group()) for x in re.finditer(chr(96) + "+", original)] + [2]) + 1)
        lines += [f"## event_{n:06d} [{clock(start)}–{clock(start+length)}]",
                  "", fence, original, fence, ""]
    if count == 0:
        raise VerbatimError("JSON3_EMPTY_TEXT")
    return "\n".join(lines) + "\n", len(events), count


FLASH_PROMPT = """Gemini 3.8 Flash — 원문 전사 전용
대화형 AGY에 소리가 포함된 동영상 파일을 직접 첨부하세요.
실제로 오디오를 들을 수 없다면 AUDIO_NOT_OBSERVED라고 답하세요.
JSON3 자막은 불완전할 수 있는 참조일 뿐 원음 증거가 아닙니다.
영상 전체에서 실제 들리는 모든 발화를 해당 언어 그대로 시간순 기록하세요.
인사, 반복, 감탄, 정정, 광고, 군더더기 말도 빠뜨리지 마세요.
요약, 번역, 의역, 19섹션 보고서나 주제 분류를 하지 마세요.
각 구간마다 [시작–종료 시각], 발화 전문, 들리지 않는 부분을 표시하세요.
화면의 코드, 프롬프트, UI 설정은 [화면]으로 발화와 분리해 기록하세요.
화면 보존이 필요한 장면은 캡처 시각을 명시하세요.
분량 한계로 전 구간을 다루지 못하면 완료한 시간 범위를 명시하세요.
"""

ARGON_PROMPT = """Gemini 4 Argon — 원문 누락 교정 전용
대화형 AGY에 소리 포함 원본 영상과 Flash 전사 및 JSON3를 함께 제공하세요.
실제로 들은 근거가 없다면 AUDIO_NOT_OBSERVED라고 명시하세요.
Flash가 빠뜨린 발화, 반복, 부정, 정정, 명령어, 수치를 찾아 보완하세요.
추측으로 발화나 화면 텍스트를 만들어내지 마세요.
[청취 불명확]과 [화면 판독 불가]를 명확히 구분하세요.
각 차이와 수정 전문에 실제 시각을 기록하세요.
요약이나 19섹션 분석으로 바꾸지 마세요.
최종 결과는 검증된 사실이 아니라 REVIEW_REQUIRED 초안입니다.
"""


def prepare(root, manifest):
    paths, out = inputs(root, manifest)
    raw = paths["subtitle_json3"].read_bytes()
    text, total, used = render_captions(raw)
    create_immutable(out / "original_captions.json3", raw)
    create_immutable(out / "captions_unedited.md", text.encode("utf-8"))
    create_immutable(out / "FLASH_INSTRUCTIONS.txt", FLASH_PROMPT.encode("utf-8"))
    create_immutable(out / "ARGON_INSTRUCTIONS.txt", ARGON_PROMPT.encode("utf-8"))
    return {"status": "VERBATIM_PREPARED", "caption_events": used,
            "raw_events": total, "audio_verified": False, "actual_agy_calls": 0}


def _create_remux_streams(output, in_streams):
    """Copy codec parameters without looking up a matching encoder.

    The PyAV 17.1.0 Windows wheel decodes AV1 with libdav1d but does not
    expose a libdav1d encoder. The opaque=True template path is intended for
    remuxing pre-encoded packets, not for decoding and re-encoding frames.
    """
    return [
        output.add_stream_from_template(stream, opaque=True)
        for stream in in_streams
    ]


def playback(root, manifest):
    """Repackage silent video + AAC into an interactive AGY audiovisual MP4."""
    paths, out = inputs(root, manifest)
    import av
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "playback_with_audio.mp4"
    tmp = out / ".playback.partial.mp4"
    if dest.exists() or dest.is_symlink() or tmp.exists() or tmp.is_symlink():
        raise VerbatimError("PLAYBACK_EXISTS")
    try:
        with ExitStack() as stack:
            vid = stack.enter_context(av.open(str(paths["primary_video"])))
            aud = stack.enter_context(av.open(str(paths["audio"])))
            if not vid.streams.video or not aud.streams.audio:
                raise VerbatimError("STREAM_MISSING")
            output = stack.enter_context(av.open(str(tmp), "w", format="mp4"))
            in_streams = (vid.streams.video[0], aud.streams.audio[0])
            out_streams = _create_remux_streams(output, in_streams)
            iters = [iter(vid.demux(in_streams[0])), iter(aud.demux(in_streams[1]))]
            heap = []

            def add_one(index):
                for packet in iters[index]:
                    if packet.size == 0:
                        continue
                    ts = packet.dts if packet.dts is not None else packet.pts
                    when = float(ts * packet.time_base) if ts is not None else 0.0
                    heapq.heappush(heap, (when, index, packet))
                    return

            add_one(0)
            add_one(1)
            while heap:
                _, index, packet = heapq.heappop(heap)
                packet.stream = out_streams[index]
                output.mux(packet)
                add_one(index)
        with av.open(str(tmp)) as check:
            if len(check.streams.video) != 1 or len(check.streams.audio) != 1:
                raise VerbatimError("PLAYBACK_STREAM_INVALID")
        os.link(tmp, dest)  # no-clobber same-filesystem publication
        return {"status": "PLAYBACK_CREATED", "audio_stream": True,
                "reencoded": False, "actual_agy_calls": 0}
    finally:
        if tmp.exists():
            tmp.unlink()



def _compatible_ffmpeg_command(ffmpeg, video, audio, destination):
    """Encode only video; preserve AAC bitstream with an explicit stream map."""
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-n",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-pix_fmt", "yuv420p", "-tag:v", "avc1",
        "-c:a", "copy", "-movflags", "+faststart", str(destination),
    ]


def playback_compatible(root, manifest):
    """Create a distinct H.264/AAC MP4; never change source AV1 or AAC."""
    paths, out = inputs(root, manifest)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VerbatimError("FFMPEG_MISSING_INSTALL_GYAN_FFMPEG")
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "playback_h264_aac.mp4"
    temp = out / ".playback_h264_aac.partial.mp4"
    if dest.exists() or dest.is_symlink() or temp.exists() or temp.is_symlink():
        raise VerbatimError("COMPATIBLE_OUTPUT_ALREADY_EXISTS")
    try:
        try:
            completed = subprocess.run(
                _compatible_ffmpeg_command(
                    ffmpeg, paths["primary_video"], paths["audio"], temp,
                ),
                capture_output=True, text=True, timeout=1800, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VerbatimError("COMPATIBLE_FFMPEG_TIMEOUT") from exc
        if completed.returncode != 0:
            # Raw stderr may contain private local paths and data.
            raise VerbatimError("COMPATIBLE_FFMPEG_FAILED")
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise VerbatimError("COMPATIBLE_FFMPEG_EMPTY_OUTPUT")

        import av
        with av.open(str(paths["primary_video"])) as original:
            source = original.streams.video[0]
            dimensions = (source.codec_context.width, source.codec_context.height)
            source_duration = (
                original.duration / av.time_base if original.duration else None
            )
        with av.open(str(temp)) as output:
            if len(output.streams.video) != 1 or len(output.streams.audio) != 1:
                raise VerbatimError("COMPATIBLE_STREAM_COUNT_MISMATCH")
            v, a = output.streams.video[0], output.streams.audio[0]
            if v.codec_context.name != "h264" or a.codec_context.name != "aac":
                raise VerbatimError("COMPATIBLE_CODEC_MISMATCH")
            if (v.codec_context.width, v.codec_context.height) != dimensions:
                raise VerbatimError("COMPATIBLE_DIMENSIONS_MISMATCH")
            duration = output.duration / av.time_base if output.duration else None
            if (not duration or not source_duration
                    or abs(duration - source_duration) > 5):
                raise VerbatimError("COMPATIBLE_DURATION_MISMATCH")
        with av.open(str(temp)) as check:
            if next(check.decode(video=0), None) is None:
                raise VerbatimError("COMPATIBLE_VIDEO_UNDECODABLE")
        with av.open(str(temp)) as check:
            if next(check.decode(audio=0), None) is None:
                raise VerbatimError("COMPATIBLE_AUDIO_UNDECODABLE")
        os.link(temp, dest)  # atomic no-clobber publication in same directory
        return {
            "status": "PLAYBACK_COMPATIBLE_CREATED",
            "video_codec": "h264", "audio_codec": "aac",
            "video_reencoded": True, "audio_copied": True,
            "duration_seconds": round(duration, 3),
            "size_mib": round(dest.stat().st_size / 1048576, 2),
            "actual_agy_calls": 0,
        }
    finally:
        if temp.exists() and not temp.is_symlink():
            temp.unlink()


def screen(root, manifest, seconds):
    """Save one owner-selected full-resolution screenshot."""
    if not isinstance(seconds, (int, float)) or not 0 <= seconds <= 21600:
        raise VerbatimError("SCREEN_TIME_INVALID")
    paths, out = inputs(root, manifest)
    import av
    with av.open(str(paths["highres_video"])) as c:
        if not c.streams.video or not c.duration:
            raise VerbatimError("VIDEO_UNAVAILABLE")
        if seconds >= c.duration / av.time_base:
            raise VerbatimError("SCREEN_OUTSIDE_VIDEO")
        stream = c.streams.video[0]
        if not stream.time_base:
            raise VerbatimError("SCREEN_TIMEBASE_MISSING")
        c.seek(int(seconds / float(stream.time_base)), stream=stream, backward=True)
        frame = next((f for f in c.decode(stream)
                      if f.time is not None and f.time >= seconds - 0.05), None)
        if frame is None or abs(float(frame.time) - seconds) > 2:
            raise VerbatimError("SCREEN_FRAME_MISSING")
        target = out / "screens" / f"at_{int(seconds*1000):010d}ms.png"
        if target.exists() or target.is_symlink():
            raise VerbatimError("SCREEN_EXISTS")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as f:
            frame.to_image().save(f, format="PNG")
        return {"status": "SCREEN_SAVED", "observed_milliseconds": round(frame.time*1000),
                "size": [frame.width, frame.height], "actual_agy_calls": 0}


def import_review(root, manifest, stage, file):
    if stage not in ("flash", "argon"):
        raise VerbatimError("STAGE_INVALID")
    _, out = inputs(root, manifest)
    file = Path(file)
    if file.is_symlink() or not file.is_file():
        raise VerbatimError("REVIEW_MISSING")
    raw = file.read_bytes()
    if not raw.strip():
        raise VerbatimError("REVIEW_EMPTY")
    raw.decode("utf-8")
    create_immutable(out / (stage + "_unverified.txt"), raw)
    return {"status": "UNVERIFIED_REVIEW_STORED", "stage": stage,
            "bytes": len(raw), "human_approved": False}


def export(root, manifest):
    _, out = inputs(root, manifest)
    files = [out / name for name in ("original_captions.json3", "captions_unedited.md",
                                    "flash_unverified.txt", "argon_unverified.txt")]
    if any(not f.is_file() or f.is_symlink() for f in files):
        raise VerbatimError("SOURCE_AND_BOTH_MODEL_DRAFTS_REQUIRED")
    rendered, _, _ = render_captions(files[0].read_bytes())
    if rendered != files[1].read_text(encoding="utf-8"):
        raise VerbatimError("ORIGINAL_CAPTIONS_TAMPERED")
    shots = sorted((out / "screens").glob("at_*.png")) if (out / "screens").is_dir() else []
    output = (
        "# 원문 중심 상세 타임라인 — REVIEW_REQUIRED\n\n"
        "AGY의 실제 청취, 전체 음성 커버리지 및 사실 일치는 별도 검수 대상입니다.\n"
        "모델 결과는 원본 JSON3나 실제 음성의 대체물이 아닙니다.\n\n"
        "## Gemini Argon 누락 검수·교정 (미승인)\n\n"
        + files[3].read_text(encoding="utf-8")
        + "\n\n## Gemini Flash 발화 전사 초안 (미승인)\n\n"
        + files[2].read_text(encoding="utf-8")
        + "\n\n## 원본 JSON3 자막 전문 (음성 검증 전)\n\n"
        + rendered
        + "\n## 필요한 화면 증거 이미지\n\n"
        + "\n".join("- " + p.relative_to(out).as_posix() for p in shots) + "\n"
    )
    create_immutable(out / "verbatim_timeline_REVIEW_REQUIRED.md", output.encode("utf-8"))
    return {"status": "VERBATIM_REVIEW_REQUIRED", "screenshots": len(shots),
            "human_approved": False, "actual_agy_calls": 0}
