"""Phase 5F.2: receipt attestation is fenced inside SQLite usage settlement."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ytresearch.batch_budget import batch_budget_status
from ytresearch.batch_queue import batch_queue_status, claim_batch_job
from ytresearch.batch_worker_contract import (
    DEFAULT_FLASH_MODEL, WorkerContractError,
    build_synthetic_stream_json_usage, build_worker_input_contract,
    create_phase5e_isolated_workspace, finish_worker_contract_job,
    materialize_and_record_nine_stages,
)
from ytresearch import batch_worker_contract as worker_mod
from ytresearch.phase5f_staging import (
    Phase5FReceiptError, RECEIPT_TABLE, finish_phase5f_receipted_job,
    stage_phase5f_receipt, verify_phase5f_receipt,
)
from ytresearch.storage import json_text


class Phase5F2SettlementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="p5f2_")
        self.addCleanup(tmp.cleanup)
        ws = create_phase5e_isolated_workspace(Path(tmp.name))
        self.root, self.sha, self.t0 = ws["root"], ws["plan_sha256"], ws["t0"]
        self.run_id = "p5f2_offline_run"
        claim = claim_batch_job(
            self.root, self.sha, owner="p5f2-owner",
            model=DEFAULT_FLASH_MODEL, now_utc=self.t0,
        )
        self.video_id = claim["video_id"]
        self.contract = build_worker_input_contract(
            self.root, plan_sha256=self.sha, video_id=self.video_id,
            run_id=self.run_id, exact_model_id=DEFAULT_FLASH_MODEL,
            lease_token=claim["lease_token"],
            lease_expires_utc=claim["lease_expires_utc"],
            attempt=1, owner="p5f2-owner",
        )
        materialize_and_record_nine_stages(
            self.root, self.contract, now_utc=self.t0 + timedelta(seconds=1),
        )

    def _usage(self):
        return build_synthetic_stream_json_usage()

    def _receipt(self):
        return stage_phase5f_receipt(
            self.root, self.contract, now_utc=self.t0 + timedelta(seconds=13),
        )

    def _finish(self):
        return finish_phase5f_receipted_job(
            self.root, self.contract, now_utc=self.t0 + timedelta(seconds=20),
            usage_source=self._usage(),
        )

    def _assert_pending(self):
        self.assertEqual(
            batch_queue_status(self.root, self.sha)["counts"].get("LEASED"), 1,
        )
        self.assertEqual(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 1,
        )

    def _store(self):
        return (self.root / "data" / "videos" / self.video_id
                / "runs" / self.run_id / ".phase5f1")

    def test_opt_in_settlement_without_receipt_is_rejected_and_rolled_back(self):
        with self.assertRaisesRegex(Phase5FReceiptError, "PHASE5F_RECEIPT_NOT_COMMITTED"):
            finish_worker_contract_job(
                self.root, self.contract, success=True,
                usage_source=self._usage(),
                require_phase5f_receipt=True,
                now_utc=self.t0 + timedelta(seconds=20),
            )
        self._assert_pending()

    def test_committed_receipt_can_settle_exactly_once(self):
        self._receipt()
        result = self._finish()
        self.assertEqual(result["status"], "SIMULATED")
        self.assertEqual(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 0
        )
        self.assertTrue(verify_phase5f_receipt(self.root, self.contract)["verified"])
        with self.assertRaises(WorkerContractError):
            self._finish()
        with closing(sqlite3.connect(self.root / "data" / "batch_dryrun_queue.sqlite3")) as db:
            count = db.execute(
                "SELECT count(*) FROM worker_usage_settlements "
                "WHERE plan_sha=? AND video_id=?",
                (self.sha, self.video_id),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_corruption_after_wrapper_precheck_detected_inside_transaction(self):
        receipt = self._receipt()
        store = self._store()
        body = json.loads(
            (store / "manifests" / receipt["receipt_sha256"]).read_text(encoding="utf-8")
        )
        target = store / "objects" / body["artifacts"][0]["sha256"]
        original = worker_mod._assert_worker_lease_locked
        mutated = {"done": False}

        def corrupt_before_settlement(db, contract, now):
            original(db, contract, now)
            if not mutated["done"]:
                target.write_bytes(b"object-corrupted-during-settlement")
                mutated["done"] = True

        with patch.object(worker_mod, "_assert_worker_lease_locked",
                          side_effect=corrupt_before_settlement):
            with self.assertRaisesRegex(
                Phase5FReceiptError, "PHASE5F_RECEIPT_OBJECT_CORRUPTED"
            ):
                self._finish()
        self.assertTrue(mutated["done"])
        self._assert_pending()

    def test_forged_manifest_omitting_an_output_fails_even_with_matching_db_digest(self):
        receipt = self._receipt()
        old_path = self._store() / "manifests" / receipt["receipt_sha256"]
        obj = json.loads(old_path.read_text(encoding="utf-8"))
        obj["artifacts"].pop()
        encoded = json_text(obj).encode("utf-8")
        sha = hashlib.sha256(encoded).hexdigest()
        (self._store() / "manifests" / sha).write_bytes(encoded)
        with closing(sqlite3.connect(self.root / "data" / "batch_dryrun_queue.sqlite3")) as db:
            db.execute(
                "UPDATE " + RECEIPT_TABLE + " SET manifest_sha=? "
                "WHERE plan_sha=? AND video_id=? AND run_id=?",
                (sha, self.sha, self.video_id, self.run_id),
            )
            db.commit()
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_RECEIPT_ARTIFACT_SET_MISMATCH"
        ):
            self._finish()
        self._assert_pending()

    def test_receipt_requirement_is_boolean_and_success_only(self):
        with self.assertRaisesRegex(WorkerContractError, "RECEIPT_REQUIREMENT_INVALID"):
            finish_worker_contract_job(
                self.root, self.contract, success=True,
                usage_source=self._usage(),
                require_phase5f_receipt="true",
                now_utc=self.t0 + timedelta(seconds=20),
            )
        with self.assertRaisesRegex(WorkerContractError, "RECEIPT_FOR_SUCCESS_ONLY"):
            finish_worker_contract_job(
                self.root, self.contract, success=False,
                provider_error_code="429", require_phase5f_receipt=True,
                now_utc=self.t0 + timedelta(seconds=20),
            )
        self._assert_pending()

    def test_parallel_settlement_only_one_succeeds(self):
        self._receipt()
        def complete(_):
            try:
                return self._finish()["status"]
            except (WorkerContractError, Phase5FReceiptError):
                return "FENCED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(complete, range(2)))
        self.assertCountEqual(outcomes, ["SIMULATED", "FENCED"])
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"].get("SIMULATED"), 1)
        self.assertEqual(
            batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 0,
        )


if __name__ == "__main__":
    unittest.main()
