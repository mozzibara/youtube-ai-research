"""Sequential timeline collection for an explicitly verified active playlist.

The 77 accessible entries are NOT assumed to equal the historical 81-entry
SQLite source registry. Raw diagnostics and local credentials stay under data/.
"""
import csv
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from .collector import VIDEO_RE, video_id_from_url
from .pipeline import collect_one, ensure_source
from .progress import report
from .storage import atomic_write, digest, json_text, read_json, video_dir
from .timeline import export_timeline, timeline_one
from .video_evidence import evidence_status, video_one

ACTIVE_REGISTRY = "data/active_playlist.json"
ACTIVE_INDEX = "research/index.json"
EXPECTED_ACCESSIBLE = 77
# These errors indicate a potentially shared YouTube/IP restriction, not
# a fact about an individual video being deleted or unavailable.
NETWORK_BLOCK_CODES = frozenset({"REQUESTBLOCKED", "IPBLOCKED"})


def _valid_video_id(value):
    return isinstance(value, str) and VIDEO_RE.fullmatch(value) is not None


def _safe_code(error):
    text = str(error)
    match = re.match(r"([A-Z][A-Z0-9_]{2,70})\s*:", text)
    return match.group(1) if match else type(error).__name__.upper()


def _youtube_playlist_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in
            ("youtube.com", "www.youtube.com", "m.youtube.com")
            or not parsed.path.startswith("/playlist")):
        raise ValueError("PLAYLIST_URL_INVALID: YouTube HTTPS 재생목록 URL 필요")
    return url


def sync_playlist(root: Path, playlist_url: str, *, expected=EXPECTED_ACCESSIBLE,
                  extractor=None):
    """Create a local, ordered active snapshot without deleting historic entries.

    Unavailable/deleted positions are retained separately; exact inaccessible
    IDs cannot be recovered when YouTube conceals them.
    """
    if expected < 1:
        raise ValueError("PLAYLIST_EXPECTED_INVALID")
    playlist_url = _youtube_playlist_url(playlist_url)
    if extractor is None:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                "PLAYLIST_YTDLP_MISSING: python -m pip install yt-dlp"
            ) from exc
        with yt_dlp.YoutubeDL({
            "extract_flat": "in_playlist", "skip_download": True,
            "ignoreerrors": True, "quiet": True, "no_warnings": True,
        }) as ydl:
            payload = ydl.extract_info(playlist_url, download=False)
    else:
        payload = extractor(playlist_url)
    if not isinstance(payload, dict) or payload.get("entries") is None:
        raise RuntimeError("PLAYLIST_NO_ENTRIES: 재생목록 접근 또는 형식 확인 필요")
    try:
        entries = list(payload["entries"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("PLAYLIST_NO_ENTRIES") from exc

    active, unavailable, seen = [], [], set()
    excluded_availability = {
        "private", "needs_auth", "subscriber_only", "premium_only",
    }
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            unavailable.append({"position": position, "reason": "unavailable"})
            continue
        name = (entry.get("title") or "").strip().lower()
        availability = (entry.get("availability") or "").lower()
        video_id = entry.get("id")
        if (not _valid_video_id(video_id)
                or availability in excluded_availability
                or name in ("private video", "deleted video", "[private video]",
                            "[deleted video]")):
            unavailable.append({
                "position": position,
                "reason": "restricted" if availability in excluded_availability
                else "removed_or_unknown",
            })
            continue
        if video_id in seen:
            unavailable.append({"position": position, "reason": "duplicate"})
            continue
        seen.add(video_id)
        active.append({
            "source_id": f"YT_{video_id}",
            "video_id": video_id,
            "playlist_position": position,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    if len(active) != expected:
        raise RuntimeError(
            f"PLAYLIST_COUNT_MISMATCH: verified={len(active)} expected={expected}; "
            "원본 상태가 달라졌을 수 있으니 기존 활성 목록을 덮어쓰지 않았습니다."
        )
    result = {
        "schema_version": 1,
        "playlist_url": playlist_url,
        "expected_accessible": expected,
        "accessible_count": len(active),
        "unavailable_positions": unavailable,
        "sources": active,
        "note": "77 is a verified snapshot, not the historical 81-entry registry.",
    }
    atomic_write(root / ACTIVE_REGISTRY, json_text(result))
    return result


def sync_sources_csv(root: Path, source_csv: Path, *,
                     expected=EXPECTED_ACCESSIBLE):
    """Use an existing 77-row source ledger as the primary batch input.

    Preserve all original source IDs, ordering and duplicate URL aliases.
    A duplicate video is run once, but every CSV entry stays represented.
    Unlike a live playlist lookup, this does not assert public availability.
    """
    if expected < 1:
        raise ValueError("CSV_EXPECTED_INVALID")
    source_csv = Path(source_csv)
    if not source_csv.is_absolute():
        source_csv = root / source_csv
    if not source_csv.is_file():
        raise RuntimeError(
            "SOURCES_CSV_NOT_FOUND: 기존 sources.csv의 경로를 확인하세요."
        )
    with source_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = {x.strip() for x in (reader.fieldnames or []) if x}
        if not {"source_id", "url"}.issubset(fields):
            raise ValueError("CSV_COLUMNS_INVALID: 필수 열은 source_id,url입니다")
        rows = list(reader)
    if len(rows) != expected:
        raise RuntimeError(
            f"CSV_SOURCE_COUNT_MISMATCH: rows={len(rows)} expected={expected}; "
            "기존 CSV나 활성 목록을 수정하지 않았습니다."
        )
    unique_sources = {}
    source_order, entries, seen_source_ids = [], [], set()
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"CSV_ROW_INVALID: position={number}")
        source_id = (row.get("source_id") or "").strip()
        url = (row.get("url") or "").strip()
        if not re.fullmatch(r"S\d{3,}", source_id):
            raise ValueError(f"CSV_SOURCE_ID_INVALID: position={number}")
        if source_id in seen_source_ids:
            raise ValueError(f"CSV_DUPLICATE_SOURCE_ID: position={number}")
        seen_source_ids.add(source_id)
        try:
            video_id = video_id_from_url(url)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"CSV_VIDEO_URL_INVALID: position={number}") from exc
        entries.append({
            "source_id": source_id, "video_id": video_id,
            "csv_position": number,
        })
        if video_id not in unique_sources:
            source_order.append(video_id)
            unique_sources[video_id] = {
                "source_id": source_id, "source_ids": [source_id],
                "video_id": video_id, "playlist_position": number,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        else:
            unique_sources[video_id]["source_ids"].append(source_id)

    sources = [unique_sources[video_id] for video_id in source_order]
    payload = {
        "schema_version": 1,
        "source_origin": "sources_csv",
        "source_file": source_csv.name,
        "source_sha256": digest(source_csv.read_bytes()),
        "expected_source_entries": expected,
        "source_entries_count": len(entries),
        "expected_accessible": len(sources),
        "accessible_count": len(sources),
        "unique_video_count": len(sources),
        "duplicate_source_entries": len(entries) - len(sources),
        "playlist_url": None,
        "unavailable_positions": [],
        "entries": entries,
        "sources": sources,
        "note": (
            "An existing 77-row source ledger, NOT a live assertion "
            "that every video is currently public. Collection will verify access."
        ),
    }
    # Build and validate everything before replacing any prior active snapshot.
    atomic_write(root / ACTIVE_REGISTRY, json_text(payload))
    return payload


def load_active(root: Path, *, expected=EXPECTED_ACCESSIBLE):
    file = root / ACTIVE_REGISTRY
    if not file.is_file():
        raise RuntimeError(
            "ACTIVE_PLAYLIST_MISSING: sources.csv가 있다면 sync-sources, "
            "아니면 sync-playlist를 실행하세요."
        )
    data = read_json(file)
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise RuntimeError("ACTIVE_PLAYLIST_INVALID")
    sources = data["sources"]
    csv_mode = data.get("source_origin") == "sources_csv"
    if csv_mode:
        entries = data.get("entries")
        if (not isinstance(entries, list) or len(entries) != expected
                or data.get("source_entries_count") != expected
                or data.get("expected_source_entries") != expected
                or data.get("accessible_count") != len(sources)
                or data.get("unique_video_count") != len(sources)):
            raise RuntimeError(
                "ACTIVE_CSV_COUNT_MISMATCH: 원본 CSV 77개 소스 ID와 "
                "고유 영상 인덱스의 수가 일치하지 않습니다."
            )
        alias_map = {}
        known_source_ids = set()
        for position, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise RuntimeError("ACTIVE_CSV_ENTRY_INVALID")
            sid, vid = entry.get("source_id"), entry.get("video_id")
            if (not isinstance(sid, str) or not re.fullmatch(r"S\d{3,}", sid)
                    or not _valid_video_id(vid) or sid in known_source_ids
                    or entry.get("csv_position") != position):
                raise RuntimeError("ACTIVE_CSV_ENTRY_INVALID")
            known_source_ids.add(sid)
            alias_map.setdefault(vid, []).append(sid)
    elif (len(sources) != expected
          or data.get("accessible_count") != len(sources)):
        raise RuntimeError(
            f"ACTIVE_PLAYLIST_COUNT_MISMATCH: actual={len(sources)} expected={expected}"
        )
    known = set()
    for index, item in enumerate(sources, 1):
        if not isinstance(item, dict) or not _valid_video_id(item.get("video_id")):
            raise RuntimeError("ACTIVE_PLAYLIST_INVALID_ENTRY")
        vid = item["video_id"]
        if (vid in known or item.get("url") !=
                f"https://www.youtube.com/watch?v={vid}"
                or type(item.get("playlist_position")) is not int
                or item["playlist_position"] < 1):
            raise RuntimeError("ACTIVE_PLAYLIST_DUPLICATE_OR_BAD_SOURCE")
        if csv_mode:
            aliases = alias_map.get(vid)
            if (not aliases or item.get("source_ids") != aliases
                    or item.get("source_id") != aliases[0]
                    or item.get("playlist_position") !=
                    next(x["csv_position"] for x in entries
                         if x["video_id"] == vid)):
                raise RuntimeError("ACTIVE_CSV_ALIAS_MISMATCH")
        elif item.get("source_id") != f"YT_{vid}":
            raise RuntimeError("ACTIVE_PLAYLIST_DUPLICATE_OR_BAD_SOURCE")
        known.add(vid)
    if csv_mode and known != set(alias_map):
        raise RuntimeError("ACTIVE_CSV_SOURCE_SET_MISMATCH")
    return data


def _existing_export(root: Path, video_id: str):
    folder = root / "research" / video_id
    manifest_file = folder / "manifest.json"
    transcript_file = folder / "transcript.md"
    if not manifest_file.is_file() or not transcript_file.is_file():
        return False
    manifest = read_json(manifest_file)
    if manifest.get("video_id") != video_id or not isinstance(manifest.get("chunks"), list):
        raise RuntimeError("RESEARCH_MANIFEST_INVALID")
    chunks = manifest["chunks"]
    if len(chunks) != manifest.get("chunk_count") or not chunks:
        raise RuntimeError("RESEARCH_MANIFEST_MISSING_CHUNKS")
    if sum(x.get("segment_count", 0) for x in chunks) != manifest.get("transcript_segments"):
        raise RuntimeError("RESEARCH_MANIFEST_SEGMENT_MISMATCH")
    for x in chunks:
        relpath = x.get("file", "")
        if (not isinstance(relpath, str) or not
                re.fullmatch(r"timeline/chunk_\d{3,6}\.md", relpath)
                or not (folder / relpath).is_file()):
            raise RuntimeError("RESEARCH_CHUNK_MISSING")
    # Compare the published segment IDs against the manifest for completeness.
    matches = re.findall(r"(?m)^\[s\d{6} @", transcript_file.read_text(encoding="utf-8"))
    if len(matches) != manifest["transcript_segments"]:
        raise RuntimeError("RESEARCH_TRANSCRIPT_SEGMENT_MISMATCH")
    source_file = video_dir(root, video_id) / "transcript.json"
    if source_file.is_file():
        _, current_sha = ensure_source(video_dir(root, video_id))
        if current_sha != manifest.get("source_sha256"):
            raise RuntimeError("RESEARCH_SOURCE_SHA_MISMATCH")
    return True


def _source_status(root: Path, item: dict):
    vid = item["video_id"]
    folder = root / "research" / vid
    direct = evidence_status(root, vid)
    if _existing_export(root, vid):
        status = "ready_for_gpt_web"
    elif direct is not None:
        status = "video_evidence_review_required"
    elif (video_dir(root, vid) / "transcript.json").is_file():
        status = "transcript_collected"
    else:
        status = "not_started"
    review_folder = folder / "video_evidence"
    reviewed_chunks = len({
        re.sub(r"_gemini$", "", p.stem)
        for p in review_folder.glob("review_chunk_*.json")
    }) if review_folder.is_dir() else 0
    combined = None
    for name in ("combined_timeline_gemini", "combined_timeline"):
        doc = review_folder / (name + ".md")
        meta_file = review_folder / (name + ".json")
        if doc.is_file() != meta_file.is_file():
            raise RuntimeError("VIDEO_INTEGRATED_TIMELINE_INCOMPLETE")
        if meta_file.is_file():
            meta = read_json(meta_file)
            if (meta.get("video_id") != vid or
                    meta.get("text_sha256") != digest(doc.read_text(encoding="utf-8"))):
                raise RuntimeError("VIDEO_INTEGRATED_TIMELINE_TAMPERED")
            if combined is None:
                combined = doc
    return {
        "source_id": item["source_id"],
        "source_ids": item.get("source_ids", [item["source_id"]]),
        "video_id": vid,
        "playlist_position": item["playlist_position"],
        "status": status,
        "manifest": f"{vid}/manifest.json" if status == "ready_for_gpt_web" else None,
        "video_evidence": direct["video_evidence"] if direct else None,
        "video_source_access": direct["source_access"] if direct else None,
        "video_review_chunks": reviewed_chunks,
        "integrated_timeline": (
            f"{vid}/video_evidence/{combined.name}"
            if combined is not None else None
        ),
        "gpt_report": f"{vid}/final/report.md"
        if (folder / "final" / "report.md").is_file() else None,
    }


def _write_index(root: Path, registry: dict, *, errors=None):
    errors = errors or {}
    previous = {}
    if (root / ACTIVE_INDEX).exists():
        try:
            data = read_json(root / ACTIVE_INDEX)
            previous = {
                x["video_id"]: x["error_code"]
                for x in data.get("sources", [])
                if isinstance(x, dict)
                and x.get("status") in ("blocked", "paused_network")
                and isinstance(x.get("video_id"), str)
                and isinstance(x.get("error_code"), str)
            }
        except (ValueError, TypeError, KeyError):
            previous = {}
    rows = []
    for item in registry["sources"]:
        try:
            row = _source_status(root, item)
        except Exception as exc:
            row = {
                "source_id": item["source_id"],
                "source_ids": item.get("source_ids", [item["source_id"]]),
                "video_id": item["video_id"],
                "playlist_position": item["playlist_position"],
                "status": "blocked", "manifest": None,
                "gpt_report": None, "error_code": _safe_code(exc),
            }
        if row["status"] not in ("ready_for_gpt_web", "video_evidence_review_required"):
            error = errors.get(row["video_id"], row.get(
                "error_code", previous.get(row["video_id"])
            ))
            if error:
                row["status"] = (
                    "paused_network" if error in NETWORK_BLOCK_CODES else "blocked"
                )
                row["error_code"] = error
        rows.append(row)
    index = {
        "schema_version": 1, "playlist_url": registry.get("playlist_url"),
        "source_origin": registry.get("source_origin", "live_playlist"),
        "source_file": registry.get("source_file"),
        "source_sha256": registry.get("source_sha256"),
        "source_entries_count": registry.get(
            "source_entries_count", len(registry["sources"])
        ),
        "duplicate_source_entries": registry.get("duplicate_source_entries", 0),
        "expected_accessible": registry["expected_accessible"],
        "source_count": len(rows),
        "ready_for_gpt_web": sum(x["status"] == "ready_for_gpt_web" for x in rows),
        "video_evidence_review_required": sum(
            x["status"] == "video_evidence_review_required" for x in rows
        ),
        "video_review_chunks": sum(x.get("video_review_chunks", 0) for x in rows),
        "paused_network_count": sum(x["status"] == "paused_network" for x in rows),
        "network_paused": any(x["status"] == "paused_network" for x in rows),
        "final_report_count": sum(bool(x["gpt_report"]) for x in rows),
        "unavailable_positions": registry.get("unavailable_positions", []),
        "sources": rows,
        "policy_file": "WEB_GPT_ANALYSIS_PROTOCOL.md",
        "notice": "Processing status is not the same as ChatGPT Web analysis approval.",
    }
    atomic_write(root / ACTIVE_INDEX, json_text(index))
    return index


def batch_timeline(root: Path, *, expected=EXPECTED_ACCESSIBLE, max_videos=3,
                   retry_blocked=False, source_mode="transcript-first"):
    """Run sequentially, stopping network requests on shared IP blocking.

    Model extraction checkpoints are preserved. YouTube RequestBlocked/IpBlocked
    is not evidence that a specific video was deleted, made private or lacks
    captions. A previous network pause requires an explicit --retry-blocked
    for the next YouTube attempt; repeated retries are never automatic.
    """
    if max_videos < 1:
        raise ValueError("BATCH_MAX_VIDEOS_INVALID")
    if source_mode not in ("transcript-first", "agy-only", "gemini-only"):
        raise ValueError("BATCH_SOURCE_MODE_INVALID")
    registry = load_active(root, expected=expected)
    if source_mode != "transcript-first":
        return _batch_video_only(
            root, registry, provider={
                "agy-only": "agy", "gemini-only": "gemini-url"
            }[source_mode], max_videos=max_videos,
        )
    attempted, completed, errors = 0, [], {}
    persisted_blocked = {}
    if (root / ACTIVE_INDEX).is_file():
        old_index = read_json(root / ACTIVE_INDEX)
        persisted_blocked = {
            x["video_id"]: x.get("error_code", "BLOCKED_PREVIOUS_RUN")
            for x in old_index.get("sources", [])
            if isinstance(x, dict)
            and x.get("status") in ("blocked", "paused_network")
            and isinstance(x.get("video_id"), str)
        }

    # This also migrates the user's earlier three "blocked: REQUESTBLOCKED"
    # rows from the prior code to "paused_network" without touching sources.
    earlier_network_blocks = [
        vid for vid, code in persisted_blocked.items()
        if code in NETWORK_BLOCK_CODES
    ]
    if earlier_network_blocks and not retry_blocked:
        index = _write_index(root, registry)
        report(
            "[BATCH] NETWORK_PAUSED: 이전 YouTube 요청 차단이 "
            f"{len(earlier_network_blocks)}개 기록돼 있습니다. "
            "더 많은 영상에 요청하지 않았습니다.",
            error=True,
        )
        report(
            "[BATCH] 차단 원인 확인·휴지 후 명시적으로 "
            "--retry-blocked를 지정해야 네트워크 수집을 다시 시도합니다."
        )
        _report_batch_summary(index, completed, errors)
        return index

    for item in registry["sources"]:
        vid = item["video_id"]
        try:
            if _existing_export(root, vid):
                report(f"[BATCH] {vid}: 기존 완료 자료 재사용")
                continue
        except Exception as exc:
            errors[vid] = _safe_code(exc)
            report(f"[BATCH] {vid} 보관 자료 검증 실패: {errors[vid]}", error=True)
            continue
        if vid in persisted_blocked and not retry_blocked:
            report(f"[BATCH] {vid}: 이전 오류 {persisted_blocked[vid]}, "
                   "재시도하려면 --retry-blocked 사용")
            continue
        if attempted >= max_videos:
            break
        attempted += 1
        pause_network = False
        try:
            # Reuse a previously imported or collected timed transcript,
            # without re-requesting YouTube.
            if not (video_dir(root, vid) / "transcript.json").exists():
                collect_one(root, vid)
            timeline_one(root, vid)
            export_timeline(root, vid)
            if not _existing_export(root, vid):
                raise RuntimeError("BATCH_EXPORT_CHECK_FAILED")
            completed.append(vid)
            report(f"[BATCH] {vid}: 시간순 원본·모델 기록 내보내기 완료")
        except Exception as exc:
            code = _safe_code(exc)
            errors[vid] = code
            if code in NETWORK_BLOCK_CODES:
                pause_network = True
                report(
                    f"[BATCH] {vid}: {code} — IP/요청 차단 가능성. "
                    "다른 영상의 YouTube 자막 수집을 중단합니다.",
                    error=True,
                )
            else:
                report(f"[BATCH] {vid}: {code} (다음 영상 계속)", error=True)
        finally:
            _write_index(root, registry, errors=errors)
        if pause_network:
            break

    index = _write_index(root, registry, errors=errors)
    _report_batch_summary(index, completed, errors)
    return index


def _batch_video_only(root, registry, *, provider, max_videos):
    """Explicit video-only work: never call youtube_transcript_api."""
    errors, completed, attempted = {}, [], 0
    for item in registry["sources"]:
        vid = item["video_id"]
        try:
            row = _source_status(root, item)
            if row["status"] in (
                "ready_for_gpt_web", "video_evidence_review_required"
            ):
                report(f"[VIDEO-BATCH] {vid}: existing research reused")
                continue
            if attempted >= max_videos:
                break
            attempted += 1
            video_one(root, vid, provider=provider, mode="full")
            completed.append(vid)
        except Exception as exc:
            errors[vid] = _safe_code(exc)
            report(
                f"[VIDEO-BATCH] {vid}: {errors[vid]}. "
                "Video access must be demonstrated, not assumed.",
                error=True,
            )
        finally:
            _write_index(root, registry, errors=errors)
    index = _write_index(root, registry, errors=errors)
    report("[이번 실행에서 영상 직접 조사 자료를 수집한 소스 목록]")
    report(", ".join(completed) if completed else "(없음)")
    report("[아직 검수 완료되지 않은 소스 목록]")
    pending = [x["video_id"] for x in index["sources"]
               if not x.get("gpt_report")]
    report(", ".join(pending) if pending else "(없음)")
    report(
        f"[VIDEO-BATCH] 신규 영상 기록={len(completed)}, "
        f"검수 대기={index['video_evidence_review_required']}, "
        f"실패={len(errors)}. 자막 세그먼트는 생성하지 않았습니다."
    )
    return index


def _report_batch_summary(index, completed, errors):
    incomplete = [
        x["video_id"] for x in index["sources"]
        if x["status"] != "ready_for_gpt_web"
    ]
    report("[이번 실행에서 타임라인 추출 완료한 소스 목록]")
    report(", ".join(completed) if completed else "(없음; 캐시 사용 또는 미실행)")
    report("[아직 타임라인 추출이 완료되지 않은 소스 목록]")
    report(", ".join(incomplete) if incomplete else "(없음)")
    report(
        f"[BATCH] 준비={index['ready_for_gpt_web']}/{len(index['sources'])}, "
        f"이번 실행={len(completed)}, 실패={len(errors)}, "
        f"네트워크 차단={index.get('paused_network_count', 0)}, "
        f"미완료={len(incomplete)}"
    )


def publish_batch(root: Path, *, expected=EXPECTED_ACCESSIBLE,
                  commit=False, push=False, acknowledge_rights=False,
                  allow_whitespace_warnings=False):
    """Publish allowlisted research files with explicit opt-in and staged recovery.

    Windows Git may emit UTF-8 output even when the process locale uses CP949.
    A failed diff --check may leave approved paths staged; only those exact
    paths can be resumed. Original AGY notes are not rewritten to remove
    whitespace or change the contents of cited evidence.
    """
    if push and not commit:
        raise RuntimeError("PUBLISH_PUSH_REQUIRES_COMMIT")
    registry = load_active(root, expected=expected)
    index = _write_index(root, registry)
    targets = [
        "research/index.json",
        "research/EXTRACTION_POLICY.md",
        "research/WEB_GPT_ANALYSIS_PROTOCOL.md",
    ]
    ready = [row for row in index["sources"]
             if row["status"] == "ready_for_gpt_web"]
    for row in ready:
        vid = row["video_id"]
        folder = root / "research" / vid
        manifest = read_json(folder / "manifest.json")
        # Strict file allowlist: never recursively add arbitrary directories.
        files = ["manifest.json", "transcript.md"] + [
            entry["file"] for entry in manifest["chunks"]
        ]
        for optional in ("final/report.md", "final/report_metadata.json"):
            if (folder / optional).is_file():
                files.append(optional)
        targets.extend("research/" + vid + "/" + file for file in files)
    # Publish only code-owned, hashed model-video evidence with a clear
    # review-required provenance label; never recursively stage directories.
    for row in index["sources"]:
        vid = row["video_id"]
        folder = root / "research" / vid / "video_evidence"
        if not folder.is_dir():
            continue
        for meta_file in sorted(folder.glob("*.json")):
            if not re.fullmatch(r"(?:full|review_chunk_\d{3}|combined_timeline)(?:_gemini)?\.json",
                                meta_file.name):
                continue
            body_file = meta_file.with_suffix(".md")
            if not body_file.is_file():
                raise RuntimeError("PUBLISH_VIDEO_EVIDENCE_INCOMPLETE")
            meta = read_json(meta_file)
            if (meta.get("video_id") != vid or
                    digest(body_file.read_text(encoding="utf-8")) != meta.get("text_sha256")):
                raise RuntimeError("PUBLISH_VIDEO_EVIDENCE_INVALID")
            targets.extend([
                "research/" + vid + "/video_evidence/" + body_file.name,
                "research/" + vid + "/video_evidence/" + meta_file.name,
            ])
    targets = [p for p in dict.fromkeys(targets) if (root / p).exists()]
    allowed = set(targets)
    result = {
        "count": len(ready), "paths": targets,
        "committed": False, "pushed": False,
    }
    report(f"[PUBLISH] 검토 대상 영상 {result['count']}개; 경로 {len(targets)}개")
    for path in targets:
        report("  " + path)
    if not commit:
        report("[PUBLISH] 미게시 미리보기입니다. 검토 후 --commit 사용")
        return result
    if not acknowledge_rights:
        raise RuntimeError(
            "PUBLISH_RIGHTS_CONFIRMATION_REQUIRED: 전체 자막의 재게시 권한 확인 후 "
            "--acknowledge-rights 사용"
        )

    def git(*args, strict=True):
        # Specify decoding explicitly. On Windows Git can write UTF-8 while
        # subprocess.run(text=True) otherwise uses the Korean CP949 locale.
        proc = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false", *args],
            encoding="utf-8", errors="replace", capture_output=True,
            check=False,
        )
        if strict and proc.returncode:
            # Never print arbitrary Git stdout/stderr: its text may contain
            # private source excerpts, remote URLs or credentials.
            raise RuntimeError(
                f"PUBLISH_GIT_ERROR: command={args[0]} "
                f"exit_code={proc.returncode}; "
                "로컬에서 Git 상태와 오류를 확인하세요."
            )
        return proc

    branch = git("branch", "--show-current").stdout.strip()
    if branch in ("main", "master", ""):
        raise RuntimeError("PUBLISH_PROTECTED_BRANCH: 작업 브랜치 필요")

    staged_before = git("diff", "--cached", "--name-only").stdout.splitlines()
    unsafe_before = [p for p in staged_before if p not in allowed]
    if unsafe_before:
        raise RuntimeError(
            f"PUBLISH_UNRELATED_STAGED_CHANGES: 허용 목록 외 staged "
            f"파일 {len(unsafe_before)}개. 자동으로 초기화하지 않았습니다."
        )
    if staged_before:
        report(
            f"[PUBLISH] 이전 중단으로 남은 staged 파일 {len(staged_before)}개 재사용"
        )

    for path in targets:
        candidate = root / path
        if not path.startswith("research/") or not candidate.is_file():
            raise RuntimeError("PUBLISH_UNSAFE_PATH")
        # Check each path component for symlinks, not only final file.
        while candidate != root:
            if candidate.is_symlink():
                raise RuntimeError("PUBLISH_UNSAFE_SYMLINK")
            candidate = candidate.parent

    git("add", "--", *targets)
    staged = git("diff", "--cached", "--name-only").stdout.splitlines()
    if not staged:
        report("[PUBLISH] Git에 새로운 변경 내용이 없습니다.")
        return result
    if any(path not in allowed for path in staged):
        raise RuntimeError("PUBLISH_UNEXPECTED_STAGED_PATH")

    whitespace = git("diff", "--cached", "--check", strict=False)
    if whitespace.returncode:
        # A Git whitespace warning uses "path:line: trailing whitespace."
        # followed by a source line. Never output the source line.
        issue_re = re.compile(
            r"^(.+?):(\d+): "
            r"(trailing whitespace\.|new blank line at EOF\.|"
            r"space before tab in indent\.)$"
        )
        issues, unrelated_lines = [], []
        for line in whitespace.stdout.splitlines():
            if not line.strip():
                continue
            match = issue_re.match(line)
            if match:
                issues.append((match.group(1), match.group(2), match.group(3)))
            elif not line.startswith("+"):
                unrelated_lines.append(line)
        # Do not suppress Git failures that cannot be classified safely.
        if (not issues or unrelated_lines or whitespace.stderr.strip()
                or any(path not in allowed for path, _, _ in issues)):
            raise RuntimeError(
                f"PUBLISH_GIT_CHECK_FAILED: exit_code={whitespace.returncode}; "
                "원인을 알 수 없어 커밋하지 않았습니다."
            )
        report(f"[PUBLISH] Git 공백 경고 {len(issues)}건 발견 (원본 무변경)")
        for path, lineno, category in issues[:8]:
            report(f"[PUBLISH] 공백 경고: {path}:{lineno} ({category})")
        if len(issues) > 8:
            report(f"[PUBLISH] 추가 공백 경고 {len(issues)-8}건 생략")
        if not allow_whitespace_warnings:
            raise RuntimeError(
                "PUBLISH_WHITESPACE_WARNINGS: 원본의 공백은 변경하지 않았습니다. "
                "검토 후 --allow-whitespace-warnings를 지정하면 내용 수정 없이 "
                "커밋할 수 있습니다."
            )
        report(
            "[PUBLISH] 명시적 승인으로 공백 경고만 허용하고 "
            "자막/AGY 기록을 수정하지 않습니다."
        )

    git("commit", "-m", "docs: publish verified chronological research source batch")
    result["committed"] = True
    if push:
        git("push", "-u", "origin", branch)
        result["pushed"] = True
    return result
