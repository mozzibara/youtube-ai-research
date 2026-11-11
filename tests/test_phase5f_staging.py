"""Offline Phase 5F.1 object receipts: crash, tamper and replay contract tests."""
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ytresearch.batch_budget import batch_budget_status
from ytresearch.batch_queue import (
    batch_queue_status, claim_batch_job,
)
from ytresearch.batch_worker_contract import (
    DEFAULT_FLASH_MODEL, WorkerContractError,
    build_synthetic_stream_json_usage, build_worker_input_contract,
    create_phase5e_isolated_workspace, materialize_and_record_nine_stages,
)
from ytresearch.phase5f_offline import run_phase5f_offline_next
from ytresearch.phase5f_staging import (
    Phase5FReceiptError, RECEIPT_TABLE,
    finish_phase5f_receipted_job, stage_phase5f_receipt,
    verify_phase5f_receipt,
)


class Phase5F1ReceiptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="p5f1_")
        self.addCleanup(tmp.cleanup)
        work = create_phase5e_isolated_workspace(Path(tmp.name))
        self.root, self.sha, self.t0 = (
            work["root"], work["plan_sha256"], work["t0"],
        )
        self.video_id = f"{1:011d}"
        self.run_id = "p5f1_synthetic_run_1"

    def _contract(self, *, run_id=None, owner="p5f1-owner"):
        claim = claim_batch_job(
            self.root, self.sha, owner=owner,
            model=DEFAULT_FLASH_MODEL, now_utc=self.t0,
        )
        contract = build_worker_input_contract(
            self.root, plan_sha256=self.sha, video_id=claim["video_id"],
            run_id=run_id or self.run_id,
            exact_model_id=DEFAULT_FLASH_MODEL,
            lease_token=claim["lease_token"],
            lease_expires_utc=claim["lease_expires_utc"],
            attempt=1, owner=owner,
        )
        return contract

    def _materialize(self, contract):
        materialize_and_record_nine_stages(
            self.root, contract, now_utc=self.t0 + timedelta(seconds=1),
        )

    def _receipt(self, contract, *, fault=None):
        return stage_phase5f_receipt(
            self.root, contract,
            now_utc=self.t0 + timedelta(seconds=13),
            fault_after=fault,
        )

    def _store(self, contract):
        return (self.root / "data" / "videos" / contract["video_id"]
                / "runs" / contract["run_id"] / ".phase5f1")

    def _settle(self, contract, *, at=20):
        return finish_phase5f_receipted_job(
            self.root, contract,
            now_utc=self.t0 + timedelta(seconds=at),
            usage_source=build_synthetic_stream_json_usage(),
        )

    def test_complete_queue_worker_requires_verified_receipt(self):
        result = run_phase5f_offline_next(
            self.root, self.sha, owner="p5f1-owner", run_id=self.run_id,
            now_utc=self.t0,
        )
        self.assertEqual(result["status"], "SIMULATED")
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["SIMULATED"], 1)
        with closing(sqlite3.connect(self.root / "data" / "batch_dryrun_queue.sqlite3")) as db:
            record = db.execute(
                "SELECT manifest_sha FROM " + RECEIPT_TABLE
                + " WHERE plan_sha=? AND video_id=?",
                (self.sha, self.video_id),
            ).fetchone()
        self.assertIsNotNone(record)
        self.assertEqual(len(record[0]), 64)
        self.assertFalse(result["private_data_published"])
        self.assertNotIn("lease_token", json.dumps(result))

    def test_snapshot_idempotence_and_manifest_identity(self):
        contract = self._contract()
        self._materialize(contract)
        first = self._receipt(contract)
        second = self._receipt(contract)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        self.assertGreaterEqual(first["artifact_count"], 10)
        self.assertEqual(
            verify_phase5f_receipt(self.root, contract)["receipt_sha256"],
            first["receipt_sha256"],
        )
        data = json.loads(
            (self._store(contract) / "manifests" / first["receipt_sha256"])
            .read_text(encoding="utf-8")
        )
        self.assertEqual(data["run_id"], contract["run_id"])
        self.assertEqual(data["attempt"], 1)
        self.assertNotIn(contract["lease_token"], json.dumps(data))
        self.assertEqual(
            len({r["relpath"] for r in data["artifacts"]}),
            len(data["artifacts"]),
        )

    def test_crash_before_receipt_commit_is_not_completed(self):
        contract = self._contract()
        self._materialize(contract)
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_INJECTED_CRASH_AFTER_OBJECTS"
        ):
            self._receipt(contract, fault="objects")
        self.assertTrue(any((self._store(contract) / "objects").iterdir()))
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_RECEIPT_NOT_COMMITTED"
        ):
            verify_phase5f_receipt(self.root, contract)
        self.assertGreater(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 0
        )
        self.assertFalse(self._receipt(contract)["replayed"])

    def test_crash_after_db_commit_can_be_replayed_and_settled_once(self):
        contract = self._contract()
        self._materialize(contract)
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_INJECTED_CRASH_AFTER_COMMIT"
        ):
            self._receipt(contract, fault="db_commit")
        self.assertTrue(verify_phase5f_receipt(self.root, contract)["verified"])
        self.assertTrue(self._receipt(contract)["replayed"])
        result = self._settle(contract)
        self.assertEqual(result["status"], "SIMULATED")
        self.assertEqual(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 0
        )
        with self.assertRaises(WorkerContractError):
            self._settle(contract)
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["SIMULATED"], 1)

    def test_tampered_snapshot_object_rejected(self):
        contract = self._contract()
        self._materialize(contract)
        result = self._receipt(contract)
        manifest = json.loads(
            (self._store(contract) / "manifests" / result["receipt_sha256"])
            .read_text(encoding="utf-8")
        )
        digest = manifest["artifacts"][0]["sha256"]
        (self._store(contract) / "objects" / digest).write_bytes(b"tampered")
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_RECEIPT_OBJECT_CORRUPTED"
        ):
            verify_phase5f_receipt(self.root, contract)

    def test_source_tampered_after_snapshot_rejected_before_settlement(self):
        contract = self._contract()
        self._materialize(contract)
        self._receipt(contract)
        original = (self.root / "data" / "videos" / self.video_id
                    / "runs" / self.run_id / "final_report.md")
        original.write_text("changed original output", encoding="utf-8")
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_RECEIPT_SOURCE_CHANGED"
        ):
            verify_phase5f_receipt(self.root, contract)
        with self.assertRaises(Phase5FReceiptError):
            self._settle(contract)
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["LEASED"], 1)

    def test_missing_manifest_and_wrong_run_identity_fail_closed(self):
        contract = self._contract()
        self._materialize(contract)
        receipt = self._receipt(contract)
        path = self._store(contract) / "manifests" / receipt["receipt_sha256"]
        path.unlink()
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_RECEIPT_MANIFEST_MISSING"
        ):
            verify_phase5f_receipt(self.root, contract)
        different = dict(contract, run_id="other_synthetic_run")
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_RECEIPT_NOT_COMMITTED"
        ):
            verify_phase5f_receipt(self.root, different)

    def test_incomplete_stage_chain_cannot_receive_receipt(self):
        contract = self._contract()
        with self.assertRaises(WorkerContractError):
            self._receipt(contract)
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["LEASED"], 1)

    def test_expired_original_lease_cannot_settle_saved_receipt(self):
        contract = self._contract()
        self._materialize(contract)
        self._receipt(contract)
        with self.assertRaises(WorkerContractError):
            self._settle(contract, at=31 * 60)
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"]["LEASED"], 1)

    def test_invalid_fault_point_and_removed_output_not_committed(self):
        contract = self._contract()
        self._materialize(contract)
        with self.assertRaisesRegex(Phase5FReceiptError, "PHASE5F_RECEIPT_FAULT_INVALID"):
            self._receipt(contract, fault="before_anything")
        (self.root / "data" / "videos" / self.video_id
         / "runs" / self.run_id / "frames_manifest.json").unlink()
        with self.assertRaises(WorkerContractError):
            self._receipt(contract)
        with self.assertRaisesRegex(Phase5FReceiptError, "PHASE5F_RECEIPT_NOT_COMMITTED"):
            verify_phase5f_receipt(self.root, contract)

    def test_receipts_remain_private_under_ignored_data(self):
        contract = self._contract()
        self._materialize(contract)
        receipt = self._receipt(contract)
        folder = self._store(contract)
        self.assertIn("/data/videos/", folder.as_posix())
        self.assertTrue((folder / "manifests" / receipt["receipt_sha256"]).is_file())
        self.assertFalse((self.root / ".git").exists())


if __name__ == "__main__":
    unittest.main()
