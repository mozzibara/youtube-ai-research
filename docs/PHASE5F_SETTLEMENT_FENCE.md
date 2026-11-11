# Phase 5F.2 — transactional receipt attestation

This milestone moves the Phase 5F.1 receipt verification **inside the same SQLite `BEGIN IMMEDIATE` transaction** used for budget settlement and queue status mutation.

The optional `require_phase5f_receipt=True` flag on the Phase 5E `finish_worker_contract_job` function is used by the offline Phase 5F receipt wrapper. When set, the transaction verifies:

1. A committed receipt exists for exactly the same plan, video, run ID and queue attempt.
2. The manifest's SHA-256 and schema match the immutable receipt row and worker identity.
3. The complete set of nine-stage output files and final checkpoint is present; no missing, duplicate or extra receipt item can masquerade as complete.
4. Every snapshot SHA-256 and byte count matches the object and, for settlement, the current original stage output.
5. The original lease token, owner, expiry and frozen budget policy are checked again in that writer transaction before usage settlement.

An unverified receipt, conflicting identity, changed file or concurrent settlement attempt must roll back without debiting the reservation or marking `SIMULATED`. The flag is intentionally optional so historical standalone Phase 5E and 5D synthetic contract tests remain independently functional.

**Limit:** This prevents stale pre-transaction receipts from being trusted at settlement, but filesystem bytes are not protected against an untrusted writer racing the database transaction. Production would require immutable storage permissions and an isolated process/runtime. No live model, media download or extra licenses are enabled.

Run `python -m unittest discover -s tests -p test_phase5f_settlement_fence.py -v` on Windows and Ubuntu, then the full suite and Phase 5D/5E contracts.
