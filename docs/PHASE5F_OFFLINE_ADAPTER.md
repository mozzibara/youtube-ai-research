# Phase 5F.0 — offline adapter integration

**Status:** implementation in a standalone Draft PR. No actual model calls or downloads.

## What it does

The public package now has a dependency-injected, synthetic-only worker function:

`run_phase5f_offline_next(root, plan_sha256, owner=..., run_id=..., adapter=..., now_utc=...)`

It is intentionally an in-process integration of preexisting Phase 5B/5C/5E contracts. It cannot accept arbitrary video material:

1. Validate the exact local 77-item synthetic source mapping, fake video bytes, and synthetic JSON3 subtitles **before any lease or budget reservation**.
2. Atomically claim the next unprocessed synthetic job with the existing SQLite lease/budget controls.
3. Build and validate the Phase 5E input contract against that lease and its attempt number.
4. Call an injected adapter under the existing offline network/subprocess/AGY/YouTube call guard.
5. For success, materialize 9 deterministic local synthetic stages, validate actual files, normalize synthetic token usage and settle budget, with status **SIMULATED** only.
6. For synthetic 429, apply the global manual rate stop, preserving unsettled reservations; never auto-recover, auto-resume or assume zero usage.
7. For bad mode, changed input media, missing usage or unsafe adapter behavior, fail without reporting completion.

`SyntheticNineStageAdapter()` is the default. `SyntheticNineStageAdapter(simulate_rate_limit=True)` exercises the explicit 429 branch. The result never returns the lease token, underlying model payloads or private URLs.

## Local offline verification

```sh
python -m unittest discover -s tests -p test_phase5f_offline.py -v
python -m unittest discover -s tests -v
python -m ytresearch phase5e-validate
python -m ytresearch phase5d-stress --workers 12 --rounds 3
```

The new tests exercise normal completion, global 429 manual pause, retained reservations, input tampering, live-mode refusal, blocked socket access, unknown usage and multiple distinct queue claims.

## Explicit limitations

- Python socket and subprocess monkeypatching is not a security boundary against arbitrary native code and may not be thread-safe. This API is intended for controlled, deterministic test adapters, **not untrusted plugins**.
- The fake video bytes are not real decodable MP4 files. The nine stages are synthetic local artifacts and must never be described as a completed production video analysis.
- Repeated queue claims are distinct jobs, not a proof that external provider delivery is exactly once. Phase 5E still enforces run-scoped usage settlement and verified-stage replay behavior.
- Immutable/content-addressed stage staging, crash replay, real adapter authorizations and production-model settlement remain future gated work.
- No third-party distribution, license grant, private corpus publication or live API execution is authorized by this PR.

## Phase 5F.1 hardened artifact receipts

The success path now stages content-addressed copies of all nine verified stage outputs plus the final checkpoint, and writes an **atomic SQLite receipt** under the original lease and budget ownership. The worker completes usage settlement only after the receipt has been independently verified. See `docs/PHASE5F_IMMUTABLE_RECEIPTS.md`.

## Phase 5F.3 recovery interface

Read-only status and explicit original-lease resume are now available in `ytresearch/phase5f_recovery.py`. These paths operate exclusively on synthetic fixture bytes. They never store or print authorization tokens. See `docs/PHASE5F_OPERATOR_RECOVERY.md`.
