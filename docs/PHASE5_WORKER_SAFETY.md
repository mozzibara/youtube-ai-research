# Phase 5A–5E.1 safety invariants (public implementation handoff)

This document captures **implementation decisions**, not private runtime records. The private experimental ledger and original output files are intentionally absent.

## Phases and non-production semantics

| Phase | Responsibility | Must not be inferred |
| --- | --- | --- |
| 5A | Deterministic offline batch plan over a fixed source registry | A planned source was fetched or processed |
| 5B | Single-worker SQLite lease, owner, manual recovery and bounded retry | A lease is a completed model run |
| 5C | Per-model and global budget reservations, shared 429 stop | Estimated cost is the actual provider invoice |
| 5D | Nine-stage synthetic checkpoint and fault-injection stress | A synthetic stage is a verified media artifact |
| 5E | Real-file input/output manifest and usage contract validation | Verified files imply a production worker invocation |
| 5E.1 | Fencing at SQLite write time for lease ownership and Run ID | Concurrency outside SQLite is fully solved |

## Queue, budget and failure invariants

1. Each claim requires the exact active plan hash, stable source mapping, exact model identifier, a single nonexpired lease and an owner-bound token. Do not treat an ambiguous `auto` model as a fixed budget reservation.
2. Under `BEGIN IMMEDIATE`, reserve worst-case token and cost estimates before execution. Both unsettled reservations and settled usage count toward limits. Preserve the shared budget identity across plans.
3. A `429` or equivalent resource-exhausted response sets **global manual pause**. Apply the provider cooldown, never shorten an existing one, and never auto-resume. Retry is a separate operator decision; maximum attempts are bounded.
4. A failed or unknown usage response **must not** be normalized to zero. Keep its reservation and require explicit actor/reason-led reconciliation.
5. Recover expired leases manually; a stale owner or expired lease cannot write stage records or finish the job. Require the same token, owner, attempt, budget policy hash, reservation and unexpired status inside the write transaction.
6. If an injected fault occurs between budget settlement and lease update, both operations roll back. Do not allow a half-finished job or released reservation.
7. Phase 5D synthetic tables and Phase 5E file-verified tables have different evidence semantics. A `SIMULATED` queue status is never approval of a 19-section analysis.

## Artifact and Run ID verification

- Root every artifact within the approved per-video directory. Reject symlinks, traversal and external absolute paths.
- Compute actual byte count and streaming SHA-256; a string naming a digest is not sufficient.
- Enforce nine-stage ordering and prior output → next input continuity.
- Bind every stage and the final queue transition to one exact Run ID.
- Recheck manifests and lease authorization **inside** the SQLite transaction.
- Hash verification under a database lock does not prevent concurrent *filesystem* replacement. Future live integration requires immutable or content-addressed staging, atomic publish and a final rehash before recording completion.

## Phase 5F entry conditions

- No live batch worker or implicit paid model invocation in this PR.
- Connect a dependency-injected offline adapter with deterministic synthetic fixtures first.
- Verify exactly-once claim/settlement behavior, repeated execution, crash/recovery and provider 429 handling.
- Preserve private raw output, avoid printing token values, source URLs or original provider text.
- Actual source trials and paid AGY usage require explicit owner authorization and their own private runtime.

Documentation derived from private historical safety studies; this is a sanitized implementation handoff, not reproduction of the private studies.

## Phase 5F.0 implementation (Public PR #2)

An explicit synthetic-only adapter in `ytresearch/phase5f_offline.py` now connects queue claims, Phase 5E input validation, nine verified local synthetic stages, and frozen usage settlement without invoking AGY, YouTube or network tools. The adapter accepts only the precisely recognized isolated 77-source fixture and fake byte strings; real media are refused before budget reservation.

The optional synthetic 429 failure retains unknown usage reservations and enables the global manual pause. Unexpected adapter results do not advance the lease. The offline guard is for this Python code path, **not** a general operating-system sandbox. Stage execution remains simulation, not real processed media.

The end-to-end orchestration must pass its own Windows/Ubuntu regression tests before any further live integration proposal. See `docs/PHASE5F_OFFLINE_ADAPTER.md`.
