"""Atomic, validated local persistence with non-destructive raw captures."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def digest(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def atomic_write(path: Path, content: str, *, immutable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        current = path.read_text(encoding="utf-8")
        if current != content:
            raise RuntimeError(f"원본 파일 변경 감지: {path}. 기존 원본은 덮어쓰지 않습니다.")
        return
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def connect(root: Path):
    path = root / "data" / "state.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'REGISTERED',
            last_error TEXT,
            transcript_sha TEXT
        );
        CREATE TABLE IF NOT EXISTS entries (
            source_id TEXT PRIMARY KEY,
            video_id TEXT NOT NULL REFERENCES videos(video_id)
        );
    """)
    db.commit()
    return db


def update_stage(db, video_id, stage, error=None, transcript_sha=None):
    if transcript_sha is None:
        db.execute("UPDATE videos SET stage=?, last_error=? WHERE video_id=?", (stage, error, video_id))
    else:
        db.execute("UPDATE videos SET stage=?, last_error=?, transcript_sha=? WHERE video_id=?", (stage, error, transcript_sha, video_id))
    db.commit()


def video_dir(root: Path, video_id: str):
    return root / "data" / "videos" / video_id


def update_stage_at_root(root: Path, video_id: str, stage: str, error=None, transcript_sha=None):
    """Persist one stage update and always release the Windows SQLite file handle."""
    from contextlib import closing
    with closing(connect(root)) as db:
        update_stage(db, video_id, stage, error=error, transcript_sha=transcript_sha)
