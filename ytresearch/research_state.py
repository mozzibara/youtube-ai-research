"""Canonical Source -> Video -> AnalysisRun -> Report state without legacy DB mutation.

SQLite lives under ignored data/. The existing 77-source ledger is read, never
overwritten or inferred from video media. A GitHub-tracked research/index.json
may serve as a historical fallback, but its stale analysis status is NOT truth.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional

from .storage import atomic_write, json_text, read_json


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REPORT_STATES = ("DRAFT", "REVIEW_REQUIRED", "APPROVED", "PUBLISHED")
RUN_STATES = ("PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "INCONSISTENT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(root: Path) -> Path:
    return Path(root) / "data" / "research_state.sqlite3"


def open_research_db(root: Path) -> sqlite3.Connection:
    path = _db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS videos (
        video_id TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        source_origin TEXT NOT NULL,
        created_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS source_entries (
        source_id TEXT PRIMARY KEY,
        video_id TEXT NOT NULL REFERENCES videos(video_id),
        source_position INTEGER NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS analysis_runs (
        video_id TEXT NOT NULL REFERENCES videos(video_id),
        run_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
          ('PENDING','IN_PROGRESS','COMPLETED','FAILED','INCONSISTENT')),
        checkpoint_path TEXT NOT NULL,
        primary_model TEXT,
        completed_utc TEXT,
        last_stage TEXT,
        error_code TEXT,
        updated_utc TEXT NOT NULL,
        PRIMARY KEY (video_id, run_id)
    );
    CREATE TABLE IF NOT EXISTS reports (
        video_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        report_kind TEXT NOT NULL CHECK(report_kind IN ('EVIDENCE','RESEARCH_19')),
        path TEXT NOT NULL,
        review_state TEXT NOT NULL CHECK(review_state IN
           ('DRAFT','REVIEW_REQUIRED','APPROVED','PUBLISHED')),
        reviewer TEXT,
        approved_utc TEXT,
        published_utc TEXT,
        updated_utc TEXT NOT NULL,
        PRIMARY KEY(video_id, run_id, report_kind),
        FOREIGN KEY(video_id, run_id) REFERENCES analysis_runs(video_id, run_id)
    );
    """)
    db.commit()
    return db


def read_source_ledger(root: Path, *, expected: int = 77) -> Dict[str, Any]:
    root = Path(root)
    active = root / "data" / "active_playlist.json"
    if active.is_file():
        from .batch import load_active
        doc = load_active(root, expected=expected)
        origin = doc.get("source_origin", "active_playlist")
    else:
        file = root / "research" / "index.json"
        if not file.is_file():
            raise FileNotFoundError("No verified local source ledger or research/index.json")
        doc = read_json(file)
        origin = "research_index_snapshot"
        if doc.get("source_count") != expected:
            raise ValueError("INDEX_SOURCE_COUNT_MISMATCH")
    items = doc.get("sources", [])
    if not isinstance(items, list):
        raise ValueError("SOURCE_LEDGER_INVALID")
    entries: List[Dict[str, Any]] = []
    seen_sources, seen_videos = set(), set()
    for row in items:
        video_id = str(row.get("video_id", ""))
        position = row.get("playlist_position")
        if not VIDEO_ID.fullmatch(video_id) or video_id in seen_videos:
            raise ValueError("SOURCE_LEDGER_VIDEO_INVALID_OR_DUPLICATED")
        if type(position) is not int or position < 1:
            raise ValueError("SOURCE_LEDGER_POSITION_INVALID")
        ids = row.get("source_ids") or [row.get("source_id")]
        if not isinstance(ids, list) or not ids:
            raise ValueError("SOURCE_LEDGER_SOURCE_ID_MISSING")
        for offset, sid in enumerate(ids):
            if not isinstance(sid, str) or not re.fullmatch(r"S\d{3,}", sid):
                raise ValueError("SOURCE_LEDGER_SOURCE_ID_INVALID")
            if sid in seen_sources:
                raise ValueError("SOURCE_LEDGER_SOURCE_ID_REASSIGNED")
            seen_sources.add(sid)
            # For duplicated source URLs, preserve CSV ordering when active ledger
            # includes entries. Otherwise retain deterministic relative order.
            entries.append({"source_id": sid, "video_id": video_id,
                            "position": position if offset == 0 else position + offset})
        seen_videos.add(video_id)
    if len(entries) != expected:
        raise ValueError(f"SOURCE_LEDGER_COUNT_MISMATCH: {len(entries)} != {expected}")
    if len({x["position"] for x in entries}) != len(entries):
        if active.is_file() and doc.get("entries"):
            entries = [
                {"source_id": e["source_id"], "video_id": e["video_id"],
                 "position": int(e["csv_position"])}
                for e in doc["entries"]
            ]
        else:
            raise ValueError("SOURCE_LEDGER_POSITION_COLLISION")
    videos = [{
        "video_id": row["video_id"],
        "url": f"https://www.youtube.com/watch?v={row['video_id']}",
    } for row in items]
    return {"origin": origin, "entry_count": len(entries), "videos": videos, "entries": entries}


def sync_research_sources(root: Path, *, expected: int = 77) -> Dict[str, int]:
    """Idempotent append/verify, never silently reassign existing Source IDs."""
    ledger = read_source_ledger(root, expected=expected)
    with closing(open_research_db(root)) as db:
        with db:
            existing = {
                row["source_id"]: (row["video_id"], row["source_position"])
                for row in db.execute("SELECT source_id, video_id, source_position FROM source_entries")
            }
            for row in ledger["entries"]:
                old = existing.get(row["source_id"])
                if old is not None and old != (row["video_id"], row["position"]):
                    raise ValueError(f"SOURCE_ID_REASSIGNMENT_BLOCKED: {row['source_id']}")
            for video in ledger["videos"]:
                db.execute(
                    "INSERT INTO videos(video_id,url,source_origin,created_utc) "
                    "VALUES (?,?,?,?) ON CONFLICT(video_id) DO NOTHING",
                    (video["video_id"], video["url"], ledger["origin"], _now()),
                )
            for row in ledger["entries"]:
                db.execute(
                    "INSERT INTO source_entries(source_id,video_id,source_position) "
                    "VALUES (?,?,?) ON CONFLICT(source_id) DO NOTHING",
                    (row["source_id"], row["video_id"], row["position"]),
                )
        return {
            "source_entries": db.execute("SELECT COUNT(*) FROM source_entries").fetchone()[0],
            "unique_videos": db.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
        }


def record_analysis_checkpoint(root: Path, video_id: str, run_id: str) -> Dict[str, Any]:
    """Import existing offline run metadata without altering original artifacts."""
    if not VIDEO_ID.fullmatch(video_id) or not RUN_ID.fullmatch(run_id):
        raise ValueError("INVALID_VIDEO_OR_RUN_ID")
    run_dir = Path(root) / "data" / "videos" / video_id / "runs" / run_id
    path = run_dir / "multimodal_checkpoint.json"
    if not path.is_file():
        raise FileNotFoundError("CHECKPOINT_MISSING: " + str(path))
    ckpt = read_json(path)
    if ckpt.get("video_id") != video_id or ckpt.get("run_id") != run_id:
        raise ValueError("CHECKPOINT_ID_MISMATCH")
    status = str(ckpt.get("status", "PENDING"))
    if status not in RUN_STATES:
        status = "INCONSISTENT"
    required = ("frames_manifest.json", "tiered_analysis.json",
                "verification.json", "final_report.md")
    missing = [name for name in required if not (run_dir / name).is_file()]
    if status == "COMPLETED" and missing:
        status = "INCONSISTENT"
    model = ckpt.get("run_config", {}).get("primary_model")
    with closing(open_research_db(root)) as db:
        with db:
            if not db.execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,)).fetchone():
                raise ValueError("UNREGISTERED_VIDEO")
            db.execute(
                """INSERT INTO analysis_runs
                (video_id,run_id,status,checkpoint_path,primary_model,
                 completed_utc,last_stage,error_code,updated_utc)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(video_id,run_id) DO UPDATE SET
                  status=excluded.status,checkpoint_path=excluded.checkpoint_path,
                  primary_model=excluded.primary_model,completed_utc=excluded.completed_utc,
                  last_stage=excluded.last_stage,error_code=excluded.error_code,
                  updated_utc=excluded.updated_utc""",
                (video_id, run_id, status, str(path), model,
                 ckpt.get("updated_utc") if status == "COMPLETED" else None,
                 ckpt.get("last_completed_stage"),
                 ("MISSING_ARTIFACTS:" + ",".join(missing)) if missing and
                 ckpt.get("status") == "COMPLETED" else ckpt.get("failed_stage"),
                 _now()),
            )
            if (run_dir / "final_report.md").is_file() and status == "COMPLETED":
                # Never downgrade a human-approved report on a rerun or scan.
                db.execute(
                    """INSERT INTO reports
                    (video_id,run_id,report_kind,path,review_state,updated_utc)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(video_id,run_id,report_kind) DO UPDATE SET
                      path=excluded.path,updated_utc=excluded.updated_utc""",
                    (video_id, run_id, "EVIDENCE", str(run_dir / "final_report.md"),
                     "REVIEW_REQUIRED", _now()),
                )
    return {"video_id": video_id, "run_id": run_id, "status": status,
            "missing_artifacts": missing, "last_stage": ckpt.get("last_completed_stage")}


def scan_local_runs(root: Path) -> Dict[str, Any]:
    """Register only runs under currently registered videos; no media/model I/O."""
    root = Path(root)
    with closing(open_research_db(root)) as db:
        videos = [r[0] for r in db.execute("SELECT video_id FROM videos ORDER BY video_id")]
    imported, errors = [], []
    for video_id in videos:
        runs = root / "data" / "videos" / video_id / "runs"
        if not runs.is_dir():
            continue
        for subdir in sorted(runs.iterdir()):
            if not subdir.is_dir() or not RUN_ID.fullmatch(subdir.name):
                continue
            if not (subdir / "multimodal_checkpoint.json").is_file():
                continue
            try:
                imported.append(record_analysis_checkpoint(root, video_id, subdir.name))
            except (OSError, ValueError, TypeError) as exc:
                errors.append({"video_id": video_id, "run_id": subdir.name,
                               "code": type(exc).__name__})
    return {"imported_runs": imported, "errors": errors}


def snapshot_research_state(root: Path, *, output: Optional[Path] = None) -> Dict[str, Any]:
    """Safe status snapshot: stored in ignored data/, not tracked research/index."""
    with closing(open_research_db(root)) as db:
        rows = db.execute("""
          SELECT s.source_id,s.source_position,s.video_id,
                 (SELECT r.run_id FROM analysis_runs r
                  WHERE r.video_id=s.video_id
                  ORDER BY CASE r.status WHEN 'COMPLETED' THEN 0 ELSE 1 END,
                           r.updated_utc DESC LIMIT 1) AS latest_run_id,
                 (SELECT r.status FROM analysis_runs r WHERE r.video_id=s.video_id
                  ORDER BY CASE r.status WHEN 'COMPLETED' THEN 0 ELSE 1 END,
                           r.updated_utc DESC LIMIT 1) AS analysis_status,
                 (SELECT COUNT(*) FROM reports p WHERE p.video_id=s.video_id
                   AND p.report_kind='RESEARCH_19' AND
                   p.review_state IN ('APPROVED','PUBLISHED')) AS approved_reports
          FROM source_entries s ORDER BY s.source_position
        """).fetchall()
    data = {
        "schema_version": 1, "generated_utc": _now(),
        "source_entry_count": len(rows),
        "sources": [dict(r) for r in rows],
        "summary": {
            "analyzed_videos": len({r["video_id"] for r in rows if r["analysis_status"] == "COMPLETED"}),
            "approved_research_videos": len({
                r["video_id"] for r in rows if r["approved_reports"] > 0
            }),
        },
    }
    target = output or (Path(root) / "data" / "research_state_snapshot.json")
    atomic_write(target, json_text(data))
    return data


def update_report_review(root: Path, video_id: str, run_id: str, *,
                         kind: str, next_state: str, reviewer: Optional[str] = None) -> None:
    """Explicit approval only; publishing requires a previously approved report."""
    if kind not in ("EVIDENCE", "RESEARCH_19") or next_state not in REPORT_STATES:
        raise ValueError("INVALID_REPORT_KIND_OR_REVIEW_STATE")
    with closing(open_research_db(root)) as db:
        row = db.execute(
            "SELECT review_state, reviewer FROM reports WHERE video_id=? AND run_id=? AND report_kind=?",
            (video_id, run_id, kind),
        ).fetchone()
        if row is None:
            raise ValueError("REPORT_NOT_REGISTERED")
        previous = row["review_state"]
        if next_state == "APPROVED" and (
            not reviewer or previous not in ("DRAFT", "REVIEW_REQUIRED")
        ):
            raise ValueError("EXPLICIT_REVIEWER_REQUIRED")
        if next_state == "PUBLISHED" and previous != "APPROVED":
            raise ValueError("APPROVAL_REQUIRED_BEFORE_PUBLICATION")
        if previous in ("APPROVED", "PUBLISHED") and next_state in ("DRAFT", "REVIEW_REQUIRED"):
            raise ValueError("REVIEW_DOWNGRADE_FORBIDDEN")
        if kind == "RESEARCH_19" and next_state in ("APPROVED", "PUBLISHED"):
            from .quality_gate import assess_research_run
            folder = Path(root) / "data" / "videos" / video_id / "runs" / run_id
            manifest_file = folder / "research_report_19_manifest.json"
            report_file = folder / "research_report_19.md"
            if not manifest_file.is_file() or not report_file.is_file():
                raise ValueError("RESEARCH_REPORT_ARTIFACT_MISSING")
            manifest = read_json(manifest_file)
            if hashlib.sha256(report_file.read_bytes()).hexdigest() != manifest.get("report_sha256"):
                raise ValueError("RESEARCH_REPORT_HASH_MISMATCH")
            gate = assess_research_run(root, video_id, run_id, persist=False)
            if gate["structural_status"] != "PASS":
                raise ValueError("QUALITY_GATE_REQUIRED_BEFORE_APPROVAL")
            if gate["approval_status"] != "REVIEW_REQUIRED":
                raise ValueError("UNRESOLVED_CLAIM_ADJUDICATIONS_BLOCK_APPROVAL")
            # Structure and hashes do not establish claim-to-image meaning.
            # Require one explicit, version-locked reviewer decision per
            # 19-section research claim; publication must retain it.
            from .review_attestation import validate_human_claim_review
            validate_human_claim_review(
                root, video_id, run_id,
                reviewer=(reviewer or row["reviewer"] or ""),
                report_sha256=manifest["report_sha256"],
            )
        with db:
            db.execute(
                """UPDATE reports SET review_state=?, reviewer=COALESCE(?,reviewer),
                approved_utc=CASE WHEN ?='APPROVED' THEN ? ELSE approved_utc END,
                published_utc=CASE WHEN ?='PUBLISHED' THEN ? ELSE published_utc END,
                updated_utc=? WHERE video_id=? AND run_id=? AND report_kind=?""",
                (next_state, reviewer, next_state, _now(), next_state, _now(),
                 _now(), video_id, run_id, kind),
            )
