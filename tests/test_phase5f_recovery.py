"""Phase 5F.3: operator recovery requires original lease, no provider calls."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from ytresearch.batch_budget import batch_budget_status
from ytresearch.batch_queue import batch_queue_status, claim_batch_job
from ytresearch.batch_worker_contract import (
    DEFAULT_FLASH_MODEL, build_worker_input_contract,
    create_phase5e_isolated_workspace, materialize_and_record_nine_stages,
)
from ytresearch.phase5f_offline import Phase5FOfflineError
from ytresearch.phase5f_recovery import (
    Phase5FRecoveryError,
    inspect_phase5f_offline_recovery,
    resume_phase5f_offline_receipt,
)
from ytresearch.phase5f_staging import (
    Phase5FReceiptError, stage_phase5f_receipt,
)


class Phase5F3RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="phase5f3_")
        self.addCleanup(tmp.cleanup)
        ws = create_phase5e_isolated_workspace(Path(tmp.name))
        self.root, self.sha, self.t0 = ws["root"], ws["plan_sha256"], ws["t0"]
        self.video_id = f"{1:011d}"
        self.run_id = "run_phase5f3_resume"

    def _setup_claim(self):
        claimed = claim_batch_job(
            self.root, self.sha, owner="phase5f3-owner",
            model=DEFAULT_FLASH_MODEL, now_utc=self.t0,
        )
        contract = build_worker_input_contract(
            self.root, plan_sha256=self.sha, video_id=self.video_id,
            run_id=self.run_id, exact_model_id=DEFAULT_FLASH_MODEL,
            lease_token=claimed["lease_token"],
            lease_expires_utc=claimed["lease_expires_utc"],
            attempt=1, owner="phase5f3-owner",
        )
        materialize_and_record_nine_stages(
            self.root, contract, now_utc=self.t0 + timedelta(seconds=1),
        )
        return claimed, contract

    def _status(self, *, t=20, run_id=None):
        return inspect_phase5f_offline_recovery(
            self.root, self.sha, video_id=self.video_id,
            run_id=run_id or self.run_id,
            now_utc=self.t0 + timedelta(seconds=t),
        )

    def _resume(self, token, *, owner="phase5f3-owner", t=20, run_id=None):
        return resume_phase5f_offline_receipt(
            self.root, self.sha, video_id=self.video_id,
            run_id=run_id or self.run_id, owner=owner, lease_token=token,
            exact_model_id=DEFAULT_FLASH_MODEL,
            now_utc=self.t0 + timedelta(seconds=t),
        )

    def _commit(self, contract, *, fault=None):
        return stage_phase5f_receipt(
            self.root, contract, now_utc=self.t0 + timedelta(seconds=12),
            fault_after=fault,
        )

    def test_recovery_after_committed_receipt_uses_original_lease(self):
        claim, contract = self._setup_claim()
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_INJECTED_CRASH_AFTER_COMMIT"
        ):
            self._commit(contract, fault="db_commit")
        self.assertEqual(self._status()["action_code"], "ORIGINAL_LEASE_PROOF_REQUIRED")
        self.assertFalse(self._status()["auto_recovery_enabled"])
        result = self._resume(claim["lease_token"])
        self.assertEqual(result["status"], "SIMULATED")
        self.assertEqual(result["actual_agy_calls"], 0)
        self.assertEqual(result["actual_youtube_calls"], 0)
        self.assertNotIn(claim["lease_token"], json.dumps(result))
        self.assertEqual(self._status()["action_code"], "ALREADY_SETTLED")
        self.assertEqual(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 0
        )

    def test_missing_receipt_cannot_be_assumed_complete(self):
        claim, contract = self._setup_claim()
        self.assertEqual(self._status()["action_code"], "RECEIPT_ABSENT_REVALIDATION_REQUIRED")
        with self.assertRaisesRegex(
            Phase5FRecoveryError, "PHASE5F_RECOVERY_RECEIPT_NOT_VERIFIED"
        ):
            self._resume(claim["lease_token"])
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["LEASED"], 1)
        self.assertEqual(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 1
        )

    def test_precommit_crash_is_not_recoverable_without_restaging(self):
        claim, contract = self._setup_claim()
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_INJECTED_CRASH_AFTER_OBJECTS"
        ):
            self._commit(contract, fault="objects")
        self.assertEqual(self._status()["action_code"], "RECEIPT_ABSENT_REVALIDATION_REQUIRED")
        with self.assertRaises(Phase5FRecoveryError):
            self._resume(claim["lease_token"])
        self._commit(contract)
        self.assertEqual(self._status()["action_code"], "ORIGINAL_LEASE_PROOF_REQUIRED")

    def test_wrong_token_or_owner_fails_without_budget_change(self):
        claim, contract = self._setup_claim()
        self._commit(contract)
        for owner, token in (
            ("phase5f3-owner", "0" * 48),
            ("different-owner", claim["lease_token"]),
            ("phase5f3-owner", "not-a-token"),
        ):
            with self.subTest(owner=owner, token_is_real=token==claim["lease_token"]):
                with self.assertRaisesRegex(
                    Phase5FRecoveryError, "PHASE5F_RECOVERY_LEASE_PROOF_INVALID"
                ):
                    self._resume(token, owner=owner)
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["LEASED"], 1)
        self.assertEqual(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 1
        )

    def test_expired_lease_requires_manual_recovery(self):
        claim, contract = self._setup_claim()
        self._commit(contract)
        self.assertEqual(
            self._status(t=31*60)["action_code"], "LEASE_EXPIRED_MANUAL_RECOVERY"
        )
        with self.assertRaisesRegex(Phase5FRecoveryError, "PHASE5F_RECOVERY_LEASE_EXPIRED"):
            self._resume(claim["lease_token"], t=31*60)
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["LEASED"], 1)

    def test_other_run_cannot_borrow_receipt(self):
        claim, contract = self._setup_claim()
        self._commit(contract)
        wrong = "another_synthetic_run"
        self.assertEqual(
            self._status(run_id=wrong)["action_code"], "RECEIPT_ABSENT_REVALIDATION_REQUIRED"
        )
        with self.assertRaises(Phase5FRecoveryError):
            self._resume(claim["lease_token"], run_id=wrong)
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["LEASED"], 1)

    def test_duplicate_resume_after_settlement_is_fenced(self):
        claim, contract = self._setup_claim()
        self._commit(contract)
        self.assertEqual(self._resume(claim["lease_token"])["status"], "SIMULATED")
        with self.assertRaisesRegex(Phase5FRecoveryError, "PHASE5F_RECOVERY_NO_ACTIVE_LEASE"):
            self._resume(claim["lease_token"])
        self.assertEqual(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 0
        )

    def test_tampered_original_file_blocks_recovery_inspector(self):
        claim, contract = self._setup_claim()
        self._commit(contract)
        report = (self.root / "data" / "videos" / self.video_id
                  / "runs" / self.run_id / "final_report.md")
        report.write_text("tampered", encoding="utf-8")
        self.assertEqual(self._status()["action_code"], "RECEIPT_INTEGRITY_BLOCKED")
        with self.assertRaises(Phase5FRecoveryError):
            self._resume(claim["lease_token"])
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["LEASED"], 1)

    def test_live_or_private_media_cannot_use_recovery_api(self):
        claim, contract = self._setup_claim()
        self._commit(contract)
        media = (self.root / "data" / "videos" / self.video_id
                 / "media" / "video.mp4")
        media.write_bytes(b"actual-media-not-the-synthetic-fixture")
        with self.assertRaises(Phase5FOfflineError):
            self._status()
        with self.assertRaises(Phase5FOfflineError):
            self._resume(claim["lease_token"])

    def test_token_free_inspection_preclaim_and_invalid_ids(self):
        self.assertEqual(self._status()["action_code"], "NOT_CLAIMED")
        result = self._status()
        self.assertFalse(result["tokens_exposed"])
        self.assertNotIn("lease_token", json.dumps(result))
        with self.assertRaisesRegex(Phase5FRecoveryError, "PHASE5F_RECOVERY_RUN_INVALID"):
            self._status(run_id="../unsafe")


if __name__ == "__main__":
    unittest.main()
