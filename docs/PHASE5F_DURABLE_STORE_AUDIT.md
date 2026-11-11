# Phase 5F.4 — no-clobber durable objects and read-only orphan audit

## Why this change

Phase 5F.1 already used temporary files, content digests and SQLite receipts. However `Path.exists() -> os.replace()` could **overwrite an object another writer created between the check and the rename**. This release removes that race from the supported local synthetic store.

The new writer flushes file contents, calls `os.link(temp, sha_target)` for an atomic no-clobber publication, re-verifies target bytes and size, and cleans its own temporary file. A pre-existing target is **never replaced**. On POSIX, the parent directory is also fsynced. A filesystem that cannot provide an atomic link is rejected, not silently downgraded. On Windows, portable directory fsync is unavailable; the implementation does not claim full sudden-power-loss durability there.

These mechanisms improve crash consistency and concurrency, but do **not** make the objects OS-enforced read-only or protect them from privileged or other-process writers.

## Forensic read-only audit

`inspect_phase5f_offline_store(root, plan_sha256, video_id=..., run_id=...)` inspects only a verified synthetic fixture. It returns fixed codes, aggregate object and orphan counts, never tokens, source URLs or object contents. It will never delete, rename, upload, reprocess, or auto-recover data.

| Audit code | Interpretation |
| --- | --- |
| `STORE_ABSENT` | No receipt store or commit exists |
| `STORE_EMPTY` | Store exists without a committed receipt |
| `UNCOMMITTED_OBJECTS` | Crash/orphan objects exist, but **no SQLite commit**; cannot settle |
| `COMMITTED_RECEIPT_VERIFIED` | Exact receipt, complete stage set and disk SHA checks passed |
| `COMMITTED_RECEIPT_CORRUPT` | A committed receipt or object did not verify; block recovery |
| `STORE_UNSAFE` | Symlink, unexpected entry or unsafe directory; block any automatic cleanup |

At most 256 directory entries are examined per directory. Exceeding this limit returns `STORE_UNSAFE`. Unreferenced objects or manifests can exist after a legitimate precommit crash and are not automatically deleted. All cleanup requires explicit future policy and exclusive operator control.

## Cross-platform verification

```sh
python -m unittest discover -s tests -p test_phase5f_store_audit.py -v
python -m unittest discover -s tests -v
python -m ytresearch phase5e-validate
python -m ytresearch phase5d-stress --workers 12 --rounds 3
```

No real videos, model calls, token persistence, binary distribution or third-party license grant are involved.

## Phase 5F.5 integrated synthetic preflight

The standalone `python -m ytresearch phase5f-validate` command repeats seven disposable tests without reading the caller's `--root` or private source registry. It is now part of the Windows/Ubuntu public CI matrix. See `docs/PHASE5F_OFFLINE_PREFLIGHT.md`.
