"""Fail-closed public Git snapshot audit. No private source manifest is stored here."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ALLOW_EXACT = {
    ".gitattributes",
    ".gitignore",
    "README.md",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-multimodal.txt",
    "config/agents.json",
    "config/batch_budget.example.json",
    ".github/workflows/unit-tests.yml",
    ".github/workflows/phase5d-offline-stress.yml",
    "docs/PUBLIC_MIGRATION.md",
    "docs/DEVELOPMENT.md",
    "docs/PUBLIC_RUNTIME_BOUNDARY.md",
    "docs/PHASE5_WORKER_SAFETY.md",
    "docs/RESEARCH_VERIFICATION_POLICY.md",
    "docs/research/LEGACY_PRIVATE_HANDOFF.md",
    "tools/compare_multimodal_runs.py",
    "tools/check_public_snapshot.py",
}
ALLOW_PREFIX = {
    "ytresearch/": (".py",),
    "tests/": (".py",),
    ".agents/agents/": (".md",),
}
KNOWN_SYNTHETIC = re.compile(r"(?:Tst|Ytx)[0-9]{8}\Z")
YOUTUBE_LITERAL = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})"
)
PLAYLIST_LITERAL = re.compile(
    r"youtube\.com/playlist\?list=([A-Za-z0-9_-]{10,})"
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
TOKEN_PATTERNS = (
    re.compile(("AI" + "za") + r"[A-Za-z0-9_-]{24,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(("sk" + "-") + r"[A-Za-z0-9]{24,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def scan_record(path: str, content: str) -> list[str]:
    """Return safe classification codes, never secret contents or raw URLs."""
    violations: list[str] = []
    if not (
        path in ALLOW_EXACT
        or any(
            path.startswith(prefix)
            and path.endswith(exts)
            and ".." not in Path(path).parts
            for prefix, exts in ALLOW_PREFIX.items()
        )
    ):
        violations.append("NON_ALLOWLISTED_FILE")
    if path in ("ytresearch/batch.py", "ytresearch/__main__.py"):
        if "PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED" not in content:
            violations.append("RESEARCH_PUBLICATION_GATE_MISSING")
    if path.startswith(("research/", "data/", "cache/", "benchmarks/")):
        violations.append("PRIVATE_CORPUS_PATH")
    if any(pattern.search(content) for pattern in TOKEN_PATTERNS):
        violations.append("TOKEN_LIKE_LITERAL")
    for match in YOUTUBE_LITERAL.finditer(content):
        if not KNOWN_SYNTHETIC.fullmatch(match.group(1)):
            violations.append("REAL_OR_UNREVIEWED_VIDEO_URL")
            break
    for match in PLAYLIST_LITERAL.finditer(content):
        if match.group(1) != "PL" + ("0" * 32):
            violations.append("REAL_OR_UNREVIEWED_PLAYLIST_URL")
            break
    for match in EMAIL.finditer(content):
        if not re.search(r"@example\.(?:com|org|net)\Z", match.group(0)):
            violations.append("NON_EXAMPLE_EMAIL")
            break
    if ("C:" + chr(92) + "Users" + chr(92)) in content or (
        "/" + "home" + "/"
    ) in content or ("/" + "Users" + "/") in content:
        violations.append("LOCAL_ABSOLUTE_PATH")
    return sorted(set(violations))


def audit_tracked_files(root: Path) -> tuple[int, list[tuple[str, str]]]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    paths = [p.decode("utf-8", "strict") for p in proc.stdout.split(b"\0") if p]
    issues: list[tuple[str, str]] = []
    for path in paths:
        candidate = root / path
        if candidate.is_symlink() or not candidate.is_file():
            issues.append((path, "INVALID_TRACKED_FILE"))
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            issues.append((path, "NON_UTF8_OR_UNREADABLE_FILE"))
            continue
        issues.extend((path, code) for code in scan_record(path, content))
    return len(paths), issues


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    count, problems = audit_tracked_files(root)
    if problems:
        for path, code in problems:
            print(f"PUBLIC_SNAPSHOT_BLOCKED {path} {code}", file=sys.stderr)
        print(f"PUBLIC_SNAPSHOT_FAIL files={count} violations={len(problems)}")
        return 1
    print(f"PUBLIC_SNAPSHOT_PASS files={count} violations=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
