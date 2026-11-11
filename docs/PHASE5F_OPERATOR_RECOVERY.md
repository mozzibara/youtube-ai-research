# Phase 5F.3 — explicit operator restart recovery (synthetic only)

This module completes the **offline** crash-recovery path without storing a lease token in Git, a document, a public log or a report.

The read-only `inspect_phase5f_offline_recovery(root, plan_sha256, video_id=..., run_id=..., now_utc=...)` returns a safe action code without including the token, owner, source URLs or model output.

| Action code | Required operator action |
| --- | --- |
| `NOT_CLAIMED` | No recovery: regular queue claim is still pending |
| `RECEIPT_ABSENT_REVALIDATION_REQUIRED` | Previously staged objects are not a committed receipt; revalidate stages and re-stage using the original valid contract |
| `ORIGINAL_LEASE_PROOF_REQUIRED` | A committed receipt is verified; operator may explicitly supply the original lease token and owner |
| `LEASE_EXPIRED_MANUAL_RECOVERY` | Never auto-resume; investigate and recover the expired lease using existing 5B/5C procedures |
| `RECEIPT_INTEGRITY_BLOCKED` | Investigate altered or missing files, do not settle |
| `ALREADY_SETTLED` | Job is already SIMULATED; repeat settlement is forbidden |
| `MANUAL_PAUSE_OR_RECOVERY` | Follow operator-only pause/reconciliation policy |
| `LEASE_METADATA_INVALID` | Stop; inspect inconsistent lease state |

The mutation entrypoint `resume_phase5f_offline_receipt(..., owner=..., lease_token=..., exact_model_id=..., now_utc=...)` performs constant-time token comparison, reconstructs the existing Phase 5E input contract and revalidates it. The original authorized token must be provided at runtime by the operator; **it is never persisted by this module or returned**. On verified receipt, synthetic usage is settled by the Phase 5F.2 transaction that validates both content hashes and lease ownership.

The entrypoint is not a general restart engine: it cannot recover a lost token, renew an expired lease, resume arbitrary provider usage, initiate an AGY request, read real study media or publish research output. It operates on the 77-item synthetic fixture only.

## Reproducible offline tests

- A crash **after** receipt DB commit is resumable with the original lease proof.
- A crash **before** commit is never silently upgraded to completed work.
- Invalid token, owner, run ID, expired lease, tampered files, or a second settlement fail closed with reservation intact when unsettled.
- No credentials or tokens appear in returned diagnostics.

Use `python -m unittest discover -s tests -p test_phase5f_recovery.py -v` and then full Windows/Ubuntu CI, Phase 5E contracts and Phase 5D stress.

The next milestone requires a separate, locally approved environment for real model/tool discovery. No local interaction is necessary to run or validate these **synthetic** tests.
