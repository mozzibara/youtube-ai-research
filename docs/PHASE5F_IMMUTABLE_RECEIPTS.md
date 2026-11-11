# Phase 5F.1 — immutable artifact receipts and restart safety

The offline worker **does not** equate a successful file write with an approved completed job. This implementation adds a separate durable receipt layer before Phase 5E usage settlement, while preserving existing 5B/5C/5E guarantees.

## Commit ordering

1. Verify the synthetic-only source ledger, live-disabled contract, exact lease, all nine verified stage events, and final multimodal checkpoint.
2. Stream-copy every distinct stage output and final checkpoint into SHA-256-addressed objects in the ignored per-run `.phase5f1/objects/` directory. Write a temporary file, flush and `fsync`, then use atomic `os.replace`; verify the resulting object's digest and length.
3. Create a deterministic SHA-256-addressed manifest under `.phase5f1/manifests/`; no raw provider text, lease token or secret is embedded.
4. Revalidate the lease, budget reservation, nine-stage output chain and original-file digests **inside SQLite `BEGIN IMMEDIATE`**, then insert the receipt row. This database transaction is the **only** commit marker. Orphan content objects do not mean a job completed.
5. Independently check the committed manifest and all content-addressed copies before running Phase 5E usage settlement. The queue remains `SIMULATED`, never LIVE.

An existing receipt for the same plan/video/run/attempt with the same manifest hash is returned as an **idempotent replay**; a different hash cannot overwrite it. The table also enforces one run identity per queue attempt.

## Injected failure and restart behavior

The stage function has test-only `fault_after="objects"` and `fault_after="db_commit"` hooks. A crash before SQLite insertion leaves no valid receipt; safe re-staging is possible while the same lease remains valid. A crash after SQLite insertion leaves a committed receipt but **no settled job**; `finish_phase5f_receipted_job` resumes with the **original valid contract** and verified snapshots. Once the lease has been settled, replaying the completion cannot double-settle it.

Do not persist a raw lease token to Git, Actions logs or PR artifacts. The caller must retain its authorized contract in a private process/runtime. Expired leases require explicit operator recovery, never automatic reuse.

## Limits

- The implemented path is synthetic-only. It uses a bounded 16-MiB cap per output and a maximum of 48 artifacts per run.
- Directory `fsync` and hardened OS-level access controls depend on the target filesystem and operating system; this layer does not replace process isolation.
- Content-addressed paths and receipts are **tamper-evident**, not OS-enforced immutable. An attacker with write access to the runtime can corrupt them; verification must reject changed bytes.
- This is not an exactly-once external provider guarantee. There is no model call in this implementation.
- Model/media production integration and immutable artifact publication to a private storage backend require separate approval.

## Offline checks

Run `python -m unittest discover -s tests -p test_phase5f_staging.py -v`, the full suite, Phase 5E validation and Phase 5D stress on Windows and Ubuntu.

## Phase 5F.2 settlement-time checks

The final usage-settlement SQLite transaction now repeats receipt identity, manifest coverage and object digest checks while holding the writer lock. The new receipt-required flag is used only in the offline Phase 5F completion path. See `docs/PHASE5F_SETTLEMENT_FENCE.md`.
