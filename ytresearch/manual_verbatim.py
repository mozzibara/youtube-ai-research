"""Manual full-speech transcription handoff. No provider calls or batching."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import heapq
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
import re
import uuid

from .research_state import read_source_ledger

ROLES = {"primary_video", "highres_video", "audio", "subtitle_json3"}
SINGLE_INPUT_CONTRACT = "verbatim_single_v1"
ALLOWED_SINGLE_ROLES = {"primary_video", "highres_video", "audio", "subtitle_json3"}
VIDEO_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")


class VerbatimError(ValueError):
    pass


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class _ValidatedContext:
    root_resolved: Path
    manifest_path: Path
    video_id: str
    is_single: bool
    input_digest: str | None
    found: dict[str, Path]
    validated_assets: dict[str, dict]
    out_dir: Path
    manifest_contract: str = "LEGACY"


def _is_symlink_or_junction(p: Path) -> bool:
    try:
        if p.is_symlink():
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(p):
            return True
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            return True
        tag = getattr(st, "st_reparse_tag", 0)
        if tag in (0xA0000003, 0xA000000C, 0x8000001B):
            return True
        if getattr(st, "st_file_attributes", 0) & 0x400:
            return True
    except OSError:
        pass
    return False


def _is_inside_git_worktree(path: Path) -> bool:
    candidates: list[Path] = [Path(os.path.abspath(path))]
    try:
        resolved = path.resolve()
        if resolved not in candidates:
            candidates.append(resolved)
    except OSError:
        pass
    for start in candidates:
        curr = start
        while True:
            try:
                git_marker = curr / ".git"
                if git_marker.exists() or git_marker.is_symlink():
                    return True
            except OSError:
                pass
            if curr == curr.parent:
                break
            curr = curr.parent
    return False


def _validate_runtime_root(root: str | Path) -> Path:
    raw_root = Path(root)
    if _is_inside_git_worktree(raw_root):
        raise VerbatimError("PUBLIC_GIT_RUNTIME_FORBIDDEN")
    root_resolved = raw_root.resolve()
    if _is_inside_git_worktree(root_resolved):
        raise VerbatimError("PUBLIC_GIT_RUNTIME_FORBIDDEN")
    return root_resolved


def _resolve_manifest_path(root: Path, manifest_path: str | Path) -> Path:
    p = Path(manifest_path)
    if ".." in p.parts:
        raise VerbatimError("PATH_TRAVERSAL_FORBIDDEN")
    if not p.is_absolute():
        p = root / p
    return p


def _check_safe_path(
    path: Path,
    root_resolved: Path,
    *,
    required_subdir: tuple[str, ...] = (),
    within: Path | None = None,
    for_output: bool = False,
    is_manifest: bool = False,
) -> Path:
    outside_code = (
        "OUTPUT_OUTSIDE_RUNTIME"
        if for_output
        else ("MANIFEST_OUTSIDE_RUNTIME" if is_manifest else "ASSET_OUTSIDE_RUNTIME")
    )
    link_code = (
        "OUTPUT_SYMLINK"
        if for_output
        else ("MANIFEST_NOT_FOUND" if is_manifest else "ASSET_UNAVAILABLE")
    )
    if ".." in path.parts:
        raise VerbatimError("PATH_TRAVERSAL_FORBIDDEN")
    resolved = path.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise VerbatimError(outside_code)
    prefix = root_resolved
    for part in required_subdir:
        prefix = prefix / part
        if _is_symlink_or_junction(prefix):
            raise VerbatimError(link_code)
    if not resolved.is_relative_to(prefix):
        raise VerbatimError(outside_code)
    if within is not None and not resolved.is_relative_to(within):
        raise VerbatimError(outside_code)
    curr = path
    while True:
        if _is_symlink_or_junction(curr):
            raise VerbatimError(link_code)
        if curr == root_resolved:
            break
        try:
            if curr.resolve() == root_resolved:
                break
        except OSError:
            pass
        if curr == curr.parent:
            break
        curr = curr.parent
    return resolved


def _read_json_object(path: Path, error_code: str) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VerbatimError(error_code) from exc
    if not isinstance(obj, dict):
        raise VerbatimError(error_code)
    return obj


def _is_valid_execution_manifest_data(
    data: dict,
    vid: str,
    digest: str | None,
    validated_asset_info: dict[str, dict],
) -> bool:
    return (
        data.get("schema_version") == SINGLE_INPUT_CONTRACT
        and data.get("video_id") == vid
        and data.get("input_digest") == digest
        and data.get("assets") == validated_asset_info
    )


def compute_input_digest(schema: str, video_id: str, assets: dict[str, dict]) -> str:
    canonical = {
        "schema_version": schema,
        "video_id": video_id,
        "assets": {
            role: {
                "sha256": assets[role]["sha256"].lower(),
                "size_bytes": assets[role]["size_bytes"],
            }
            for role in sorted(assets.keys())
        },
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_validated_context(root, manifest_path, *, allow_legacy_numeric_v1=False) -> _ValidatedContext:
    root_resolved = _validate_runtime_root(root)
    raw_manifest = _resolve_manifest_path(root_resolved, manifest_path)
    if _is_inside_git_worktree(raw_manifest):
        raise VerbatimError("PUBLIC_GIT_RUNTIME_FORBIDDEN")

    resolved_manifest = _check_safe_path(
        raw_manifest,
        root_resolved,
        required_subdir=("data",),
        is_manifest=True,
    )
    if not resolved_manifest.is_file():
        raise VerbatimError("MANIFEST_NOT_FOUND")

    manifest = _read_json_object(resolved_manifest, "MANIFEST_INVALID")

    vid = manifest.get("video_id")
    if not isinstance(vid, str) or not VIDEO_RE.fullmatch(vid):
        raise VerbatimError("VIDEO_ID_INVALID")

    has_schema = "schema_version" in manifest
    has_contract = "contract" in manifest
    schema_version = manifest.get("schema_version")
    contract = manifest.get("contract")

    if has_schema and has_contract and schema_version != contract:
        raise VerbatimError("SCHEMA_CONFLICT")

    numeric_v1 = (allow_legacy_numeric_v1 and has_schema and not has_contract
                  and type(schema_version) is int and schema_version == 1)
    if numeric_v1:
        # Historical owner-created pilot format. Preserve legacy registry
        # and all four required asset roles. Do not reinterpret as single input.
        if manifest.get("status") != "OFFLINE_INPUT_VERIFIED":
            raise VerbatimError("LEGACY_V1_STATUS_INVALID")
        is_single = False
        manifest_contract = "LEGACY_NUMERIC_V1"
    elif has_schema or has_contract:
        raw_val = schema_version if has_schema else contract
        if not isinstance(raw_val, str) or raw_val != SINGLE_INPUT_CONTRACT:
            raise VerbatimError("SCHEMA_UNSUPPORTED")
        is_single = True
        manifest_contract = "SINGLE"
    else:
        is_single = False
        manifest_contract = "LEGACY"

    if not is_single:
        if numeric_v1:
            active = root_resolved / "data" / "active_playlist.json"
            fallback = root_resolved / "research" / "index.json"
            for ledger_path in (active, fallback):
                if ledger_path.exists() or _is_symlink_or_junction(ledger_path):
                    _check_safe_path(ledger_path, root_resolved)
            try:
                ledger = read_source_ledger(root_resolved, expected=77)
            except FileNotFoundError as exc:
                raise VerbatimError("LEGACY_SOURCE_LEDGER_MISSING") from exc
            except (ValueError, OSError, TypeError, KeyError) as exc:
                raise VerbatimError("LEGACY_SOURCE_LEDGER_INVALID") from exc
        else:
            ledger = read_source_ledger(root_resolved, expected=77)
        if vid not in {x["video_id"] for x in ledger["videos"]}:
            raise VerbatimError("VIDEO_NOT_REGISTERED")

    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise VerbatimError("ASSET_ROLES_INVALID")

    if is_single:
        asset_keys = set(assets.keys())
        if not asset_keys.issubset(ALLOWED_SINGLE_ROLES) or "primary_video" not in asset_keys:
            raise VerbatimError("ASSET_ROLES_INVALID")
        expected_keys = sorted(asset_keys)
    else:
        if set(assets) != ROLES:
            raise VerbatimError("ASSET_ROLES_INVALID")
        expected_keys = sorted(ROLES)

    found: dict[str, Path] = {}
    validated_asset_info: dict[str, dict] = {}
    for key in expected_keys:
        item = assets[key]
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise VerbatimError("ASSET_INVALID")
        if ".." in Path(item["path"]).parts:
            raise VerbatimError("PATH_TRAVERSAL_FORBIDDEN")
        path = Path(item["path"])
        if not path.is_absolute():
            path = root_resolved / path
        resolved_asset = _check_safe_path(path, root_resolved)
        if not resolved_asset.is_file():
            raise VerbatimError("ASSET_UNAVAILABLE")
        if type(item.get("size_bytes")) is not int or item["size_bytes"] < 0 or resolved_asset.stat().st_size != item["size_bytes"]:
            raise VerbatimError("ASSET_SIZE_MISMATCH")
        h = item.get("sha256")
        if not isinstance(h, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", h):
            raise VerbatimError("ASSET_HASH_INVALID")
        actual_h = sha(resolved_asset)
        if actual_h != h.lower():
            raise VerbatimError("ASSET_HASH_MISMATCH")
        found[key] = resolved_asset
        validated_asset_info[key] = {
            "sha256": actual_h,
            "size_bytes": item["size_bytes"],
        }

    if is_single and "highres_video" not in found:
        found["highres_video"] = found["primary_video"]

    if is_single:
        digest: str | None = compute_input_digest(SINGLE_INPUT_CONTRACT, vid, validated_asset_info)
        out_dir = _check_safe_path(
            root_resolved / "data" / "verbatim" / vid / digest,
            root_resolved,
            required_subdir=("data", "verbatim"),
            for_output=True,
        )
        exec_mf = out_dir / "execution_manifest.json"
        if exec_mf.exists() or _is_symlink_or_junction(exec_mf):
            _check_safe_path(
                exec_mf,
                root_resolved,
                required_subdir=("data", "verbatim"),
                within=out_dir,
                for_output=True,
            )
            data = _read_json_object(exec_mf, "EXECUTION_MANIFEST_TAMPERED")
            if not _is_valid_execution_manifest_data(data, vid, digest, validated_asset_info):
                raise VerbatimError("EXECUTION_MANIFEST_TAMPERED")
        elif out_dir.exists():
            existing = [
                p for p in out_dir.glob("**/*")
                if p.is_file()
                and not p.name.startswith(".playback")
                and not (p.name.startswith(".execution_manifest.") and p.name.endswith(".partial.json"))
            ]
            if existing:
                raise VerbatimError("EXECUTION_MANIFEST_MISSING")
    else:
        digest = None
        out_dir = _check_safe_path(
            root_resolved / "data" / "verbatim" / vid,
            root_resolved,
            required_subdir=("data", "verbatim"),
            for_output=True,
        )

    return _ValidatedContext(
        root_resolved=root_resolved,
        manifest_path=resolved_manifest,
        video_id=vid,
        is_single=is_single,
        input_digest=digest,
        found=found,
        validated_assets=validated_asset_info,
        out_dir=out_dir,
        manifest_contract=manifest_contract,
    )


def inputs(root, manifest_path):
    ctx = _load_validated_context(root, manifest_path)
    return ctx.found, ctx.out_dir


def create_immutable(path, raw, *, root_resolved: Path | None = None, within: Path | None = None):
    path = Path(path)
    if root_resolved is not None:
        _check_safe_path(
            path,
            root_resolved,
            required_subdir=("data", "verbatim"),
            within=within,
            for_output=True,
        )
    else:
        curr = path
        while True:
            if _is_symlink_or_junction(curr):
                raise VerbatimError("OUTPUT_SYMLINK")
            if curr == curr.parent:
                break
            curr = curr.parent
    expected_sha = hashlib.sha256(raw).hexdigest()
    if path.exists():
        if path.is_file() and sha(path) == expected_sha:
            return
        raise VerbatimError("OUTPUT_CONFLICT")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.partial"
    try:
        with tmp.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            if path.is_file() and sha(path) == expected_sha:
                return
            raise VerbatimError("OUTPUT_CONFLICT")
    finally:
        if tmp.exists() and not _is_symlink_or_junction(tmp):
            tmp.unlink()


def clock(ms):
    secs, frac = divmod(ms, 1000)
    return f"{secs//3600:02d}:{secs//60%60:02d}:{secs%60:02d}.{frac:03d}"


def render_captions(raw):
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise VerbatimError("JSON3_EVENTS_INVALID") from exc
    if not isinstance(data, dict):
        raise VerbatimError("JSON3_EVENTS_INVALID")
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

FLASH_PROMPT_NO_CAPTIONS = """Gemini 3.8 Flash — 원문 전사 전용
대화형 AGY에 소리가 포함된 동영상 파일을 직접 첨부하세요.
실제로 오디오를 들을 수 없다면 AUDIO_NOT_OBSERVED라고 답하세요.
제공된 JSON3 자막이 없으므로 영상의 실제 오디오에만 근거해 전사하세요.
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

ARGON_PROMPT_NO_CAPTIONS = """Gemini 4 Argon — 원문 누락 교정 전용
대화형 AGY에 소리 포함 원본 영상과 Flash 전사를 함께 제공하세요 (제공된 자막 없음).
실제로 들은 근거가 없다면 AUDIO_NOT_OBSERVED라고 명시하세요.
Flash가 빠뜨린 발화, 반복, 부정, 정정, 명령어, 수치를 찾아 보완하세요.
추측으로 발화나 화면 텍스트를 만들어내지 마세요.
[청취 불명확]과 [화면 판독 불가]를 명확히 구분하세요.
각 차이와 수정 전문에 실제 시각을 기록하세요.
요약이나 19섹션 분석으로 바꾸지 마세요.
최종 결과는 검증된 사실이 아니라 REVIEW_REQUIRED 초안입니다.
"""


def _write_execution_manifest(ctx: _ValidatedContext) -> None:
    if not ctx.is_single:
        return
    exec_manifest = {
        "schema_version": SINGLE_INPUT_CONTRACT,
        "video_id": ctx.video_id,
        "input_digest": ctx.input_digest,
        "assets": ctx.validated_assets,
    }
    raw = json.dumps(exec_manifest, indent=2, sort_keys=True).encode("utf-8")
    expected_sha = hashlib.sha256(raw).hexdigest()
    dest = _check_safe_path(
        ctx.out_dir / "execution_manifest.json",
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=ctx.out_dir,
        for_output=True,
    )
    if dest.exists() or _is_symlink_or_junction(dest):
        if dest.is_file() and not _is_symlink_or_junction(dest):
            if sha(dest) == expected_sha:
                return
            data = _read_json_object(dest, "EXECUTION_MANIFEST_TAMPERED")
            if _is_valid_execution_manifest_data(
                data, ctx.video_id, ctx.input_digest, ctx.validated_assets
            ):
                return
        raise VerbatimError("EXECUTION_MANIFEST_TAMPERED")
    _check_safe_path(
        ctx.out_dir,
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        for_output=True,
    )
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    tmp = ctx.out_dir / f".execution_manifest.{os.getpid()}.{uuid.uuid4().hex[:8]}.partial.json"
    try:
        with tmp.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, dest)
        except FileExistsError:
            if dest.is_file() and not _is_symlink_or_junction(dest) and sha(dest) == expected_sha:
                return
            raise VerbatimError("EXECUTION_MANIFEST_TAMPERED")
    finally:
        if tmp.exists() and not _is_symlink_or_junction(tmp):
            tmp.unlink()


def prepare(root, manifest):
    ctx = _load_validated_context(root, manifest)
    paths, out = ctx.found, ctx.out_dir
    has_captions = "subtitle_json3" in paths and paths["subtitle_json3"] is not None
    if has_captions:
        raw = paths["subtitle_json3"].read_bytes()
        text, total, used = render_captions(raw)
        flash_inst = FLASH_PROMPT
        argon_inst = ARGON_PROMPT
    else:
        raw = b""
        text = ""
        total, used = 0, 0
        flash_inst = FLASH_PROMPT_NO_CAPTIONS
        argon_inst = ARGON_PROMPT_NO_CAPTIONS

    _write_execution_manifest(ctx)

    if has_captions:
        create_immutable(
            out / "original_captions.json3",
            raw,
            root_resolved=ctx.root_resolved,
            within=out,
        )
        create_immutable(
            out / "captions_unedited.md",
            text.encode("utf-8"),
            root_resolved=ctx.root_resolved,
            within=out,
        )

    create_immutable(
        out / "FLASH_INSTRUCTIONS.txt",
        flash_inst.encode("utf-8"),
        root_resolved=ctx.root_resolved,
        within=out,
    )
    create_immutable(
        out / "ARGON_INSTRUCTIONS.txt",
        argon_inst.encode("utf-8"),
        root_resolved=ctx.root_resolved,
        within=out,
    )

    return {
        "status": "VERBATIM_PREPARED",
        "caption_events": used,
        "raw_events": total,
        "audio_verified": False,
        "actual_agy_calls": 0,
        "caption_available": has_captions,
    }


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
    ctx = _load_validated_context(root, manifest)
    paths, out = ctx.found, ctx.out_dir
    import av
    dest = _check_safe_path(
        out / "playback_with_audio.mp4",
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=out,
        for_output=True,
    )
    tmp = _check_safe_path(
        out / ".playback.partial.mp4",
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=out,
        for_output=True,
    )
    if dest.exists() or _is_symlink_or_junction(dest) or tmp.exists() or _is_symlink_or_junction(tmp):
        raise VerbatimError("PLAYBACK_EXISTS")
    try:
        with ExitStack() as stack:
            try:
                vid = stack.enter_context(av.open(str(paths["primary_video"])))
            except Exception as exc:
                raise VerbatimError("MEDIA_CORRUPTED") from exc

            if "audio" in paths and paths["audio"] is not None:
                try:
                    aud = stack.enter_context(av.open(str(paths["audio"])))
                except Exception as exc:
                    raise VerbatimError("MEDIA_CORRUPTED") from exc
            else:
                aud = vid

            if not vid.streams.video or not aud.streams.audio:
                raise VerbatimError("STREAM_MISSING")

            if aud.streams.audio[0].codec_context.name != "aac":
                raise VerbatimError("AUDIO_CODEC_UNSUPPORTED")

            _check_safe_path(
                out,
                ctx.root_resolved,
                required_subdir=("data", "verbatim"),
                for_output=True,
            )
            out.mkdir(parents=True, exist_ok=True)
            _write_execution_manifest(ctx)

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
        if tmp.exists() and not _is_symlink_or_junction(tmp):
            tmp.unlink()


def _compatible_ffmpeg_command(ffmpeg, video, audio, destination):
    """Encode only video; preserve AAC bitstream with an explicit stream map."""
    if audio is None or str(audio) == str(video):
        return [
            str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-n",
            "-i", str(video),
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-pix_fmt", "yuv420p", "-tag:v", "avc1",
            "-c:a", "copy", "-movflags", "+faststart", str(destination),
        ]
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
    ctx = _load_validated_context(root, manifest)
    paths, out = ctx.found, ctx.out_dir
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VerbatimError("FFMPEG_MISSING_INSTALL_GYAN_FFMPEG")

    dest = _check_safe_path(
        out / "playback_h264_aac.mp4",
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=out,
        for_output=True,
    )
    temp = _check_safe_path(
        out / ".playback_h264_aac.partial.mp4",
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=out,
        for_output=True,
    )
    if dest.exists() or _is_symlink_or_junction(dest) or temp.exists() or _is_symlink_or_junction(temp):
        raise VerbatimError("COMPATIBLE_OUTPUT_ALREADY_EXISTS")

    import av
    try:
        with av.open(str(paths["primary_video"])) as original:
            if not original.streams.video:
                raise VerbatimError("STREAM_MISSING")
    except VerbatimError:
        raise
    except Exception as exc:
        raise VerbatimError("MEDIA_CORRUPTED") from exc

    audio_path = paths.get("audio")
    target_audio = audio_path or paths["primary_video"]
    try:
        with av.open(str(target_audio)) as aud_file:
            if not aud_file.streams.audio:
                raise VerbatimError("STREAM_MISSING")
            if aud_file.streams.audio[0].codec_context.name != "aac":
                raise VerbatimError("AUDIO_CODEC_UNSUPPORTED")
    except VerbatimError:
        raise
    except Exception as exc:
        raise VerbatimError("MEDIA_CORRUPTED") from exc

    _check_safe_path(
        out,
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        for_output=True,
    )
    out.mkdir(parents=True, exist_ok=True)
    _write_execution_manifest(ctx)
    try:
        try:
            completed = subprocess.run(
                _compatible_ffmpeg_command(
                    ffmpeg, paths["primary_video"], audio_path, temp,
                ),
                capture_output=True, text=True, timeout=1800, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VerbatimError("COMPATIBLE_FFMPEG_TIMEOUT") from exc
        if completed.returncode != 0:
            raise VerbatimError("COMPATIBLE_FFMPEG_FAILED")
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise VerbatimError("COMPATIBLE_FFMPEG_EMPTY_OUTPUT")

        with av.open(str(paths["primary_video"])) as original:
            if not original.streams.video:
                raise VerbatimError("STREAM_MISSING")
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
        if temp.exists() and not _is_symlink_or_junction(temp):
            temp.unlink()


def screen(root, manifest, seconds):
    """Save one owner-selected full-resolution screenshot."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 21600:
        raise VerbatimError("SCREEN_TIME_INVALID")
    ctx = _load_validated_context(root, manifest)
    paths, out = ctx.found, ctx.out_dir
    screens_dir = _check_safe_path(
        out / "screens",
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=out,
        for_output=True,
    )
    target = _check_safe_path(
        screens_dir / f"at_{int(seconds*1000):010d}ms.png",
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=screens_dir,
        for_output=True,
    )
    if target.exists() or _is_symlink_or_junction(target):
        raise VerbatimError("SCREEN_EXISTS")
    import av
    target_video = paths.get("highres_video") or paths["primary_video"]
    try:
        with av.open(str(target_video)) as c:
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
            import io
            buf = io.BytesIO()
            frame.to_image().save(buf, format="PNG")
            _write_execution_manifest(ctx)
            create_immutable(
                target,
                buf.getvalue(),
                root_resolved=ctx.root_resolved,
                within=screens_dir,
            )
            return {"status": "SCREEN_SAVED", "observed_milliseconds": round(frame.time*1000),
                    "size": [frame.width, frame.height], "actual_agy_calls": 0}
    except VerbatimError:
        raise
    except av.error.FFmpegError as exc:
        raise VerbatimError("MEDIA_CORRUPTED") from exc


def import_review(root, manifest, stage, file):
    if stage not in ("flash", "argon"):
        raise VerbatimError("STAGE_INVALID")
    ctx = _load_validated_context(root, manifest)
    out = ctx.out_dir
    file = Path(file)
    if ".." in file.parts:
        raise VerbatimError("PATH_TRAVERSAL_FORBIDDEN")
    if not file.is_absolute():
        file = ctx.root_resolved / file
    resolved_file = _check_safe_path(file, ctx.root_resolved)
    if not resolved_file.is_file():
        raise VerbatimError("REVIEW_MISSING")
    raw = resolved_file.read_bytes()
    if not raw.strip():
        raise VerbatimError("REVIEW_EMPTY")
    raw.decode("utf-8")
    target_review = _check_safe_path(
        out / (stage + "_unverified.txt"),
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=out,
        for_output=True,
    )
    if ctx.is_single:
        rev_mf = _check_safe_path(
            out / f"{stage}_review_manifest.json",
            ctx.root_resolved,
            required_subdir=("data", "verbatim"),
            within=out,
            for_output=True,
        )
    else:
        rev_mf = None

    _write_execution_manifest(ctx)
    create_immutable(
        target_review,
        raw,
        root_resolved=ctx.root_resolved,
        within=out,
    )

    if ctx.is_single and rev_mf is not None:
        review_manifest = {
            "schema_version": SINGLE_INPUT_CONTRACT,
            "stage": stage,
            "video_id": ctx.video_id,
            "input_digest": ctx.input_digest,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
        create_immutable(
            rev_mf,
            json.dumps(review_manifest, indent=2, sort_keys=True).encode("utf-8"),
            root_resolved=ctx.root_resolved,
            within=out,
        )

    return {"status": "UNVERIFIED_REVIEW_STORED", "stage": stage,
            "bytes": len(raw), "human_approved": False}


def export(root, manifest):
    ctx = _load_validated_context(root, manifest)
    paths, out = ctx.found, ctx.out_dir
    manifest_requires_captions = ("subtitle_json3" in paths and paths["subtitle_json3"] is not None)

    model_drafts = []
    for name in ("flash_unverified.txt", "argon_unverified.txt"):
        draft = _check_safe_path(
            out / name,
            ctx.root_resolved,
            required_subdir=("data", "verbatim"),
            within=out,
            for_output=True,
        )
        if not draft.is_file():
            raise VerbatimError("SOURCE_AND_BOTH_MODEL_DRAFTS_REQUIRED")
        model_drafts.append(draft)

    if ctx.is_single:
        for stage in ("flash", "argon"):
            rev_mf = _check_safe_path(
                out / f"{stage}_review_manifest.json",
                ctx.root_resolved,
                required_subdir=("data", "verbatim"),
                within=out,
                for_output=True,
            )
            if not rev_mf.is_file():
                raise VerbatimError("REVIEW_REUSE_FORBIDDEN")
            rev_data = _read_json_object(rev_mf, "REVIEW_REUSE_FORBIDDEN")
            draft_file = out / f"{stage}_unverified.txt"
            if (rev_data.get("stage") != stage
                    or rev_data.get("input_digest") != ctx.input_digest
                    or rev_data.get("video_id") != ctx.video_id
                    or rev_data.get("response_sha256") != sha(draft_file)
                    or rev_data.get("size_bytes") != draft_file.stat().st_size):
                raise VerbatimError("REVIEW_REUSE_FORBIDDEN")

    if manifest_requires_captions:
        cap_file = _check_safe_path(
            out / "original_captions.json3",
            ctx.root_resolved,
            required_subdir=("data", "verbatim"),
            within=out,
            for_output=True,
        )
        md_file = _check_safe_path(
            out / "captions_unedited.md",
            ctx.root_resolved,
            required_subdir=("data", "verbatim"),
            within=out,
            for_output=True,
        )
        if not cap_file.is_file() or not md_file.is_file():
            raise VerbatimError("SOURCE_AND_BOTH_MODEL_DRAFTS_REQUIRED")

        expected_caption_sha = sha(paths["subtitle_json3"])
        if sha(cap_file) != expected_caption_sha:
            raise VerbatimError("ORIGINAL_CAPTIONS_TAMPERED")

        rendered, _, _ = render_captions(cap_file.read_bytes())
        if rendered != md_file.read_text(encoding="utf-8"):
            raise VerbatimError("ORIGINAL_CAPTIONS_TAMPERED")
        captions_body = rendered
    else:
        captions_body = "(자막 미제공: 영상 음성 직접 전사 대상)\n"

    screens_dir = out / "screens"
    shots = []
    if screens_dir.exists() or _is_symlink_or_junction(screens_dir):
        _check_safe_path(
            screens_dir,
            ctx.root_resolved,
            required_subdir=("data", "verbatim"),
            within=out,
            for_output=True,
        )
        if screens_dir.is_dir():
            for p in sorted(screens_dir.glob("at_*.png")):
                _check_safe_path(
                    p,
                    ctx.root_resolved,
                    required_subdir=("data", "verbatim"),
                    within=screens_dir,
                    for_output=True,
                )
                shots.append(p)

    output = (
        "# 원문 중심 상세 타임라인 — REVIEW_REQUIRED\n\n"
        "AGY의 실제 청취, 전체 음성 커버리지 및 사실 일치는 별도 검수 대상입니다.\n"
        "모델 결과는 원본 JSON3나 실제 음성의 대체물이 아닙니다.\n\n"
        "## Gemini Argon 누락 검수·교정 (미승인)\n\n"
        + model_drafts[1].read_text(encoding="utf-8")
        + "\n\n## Gemini Flash 발화 전사 초안 (미승인)\n\n"
        + model_drafts[0].read_text(encoding="utf-8")
        + "\n\n## 원본 JSON3 자막 전문 (음성 검증 전)\n\n"
        + captions_body
        + "\n## 필요한 화면 증거 이미지\n\n"
        + "\n".join("- " + p.relative_to(out).as_posix() for p in shots) + "\n"
    )
    timeline_file = _check_safe_path(
        out / "verbatim_timeline_REVIEW_REQUIRED.md",
        ctx.root_resolved,
        required_subdir=("data", "verbatim"),
        within=out,
        for_output=True,
    )
    create_immutable(
        timeline_file,
        output.encode("utf-8"),
        root_resolved=ctx.root_resolved,
        within=out,
    )
    return {"status": "VERBATIM_REVIEW_REQUIRED", "screenshots": len(shots),
            "human_approved": False, "actual_agy_calls": 0}
