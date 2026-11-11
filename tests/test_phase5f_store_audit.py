"""Phase 5F.4: no-clobber object publication and non-destructive orphan audit."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ytresearch.batch_queue import claim_batch_job
from ytresearch.batch_worker_contract import (
    DEFAULT_FLASH_MODEL, build_worker_input_contract,
    create_phase5e_isolated_workspace, materialize_and_record_nine_stages,
)
from ytresearch.phase5f_audit import inspect_phase5f_offline_store
from ytresearch.phase5f_staging import (
    Phase5FReceiptError, _atomic_content_object, _safe_directory,
    stage_phase5f_receipt,
)


class Phase5F4AuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="phase5f4_")
        self.addCleanup(tmp.cleanup)
        ws = create_phase5e_isolated_workspace(Path(tmp.name))
        self.root, self.sha, self.t0 = ws["root"], ws["plan_sha256"], ws["t0"]
        self.video_id = f"{1:011d}"
        self.run_id = "p5f4_synthetic_receipt"

    def _claim(self):
        job = claim_batch_job(
            self.root, self.sha, owner="phase5f4-owner",
            model=DEFAULT_FLASH_MODEL, now_utc=self.t0,
        )
        contract = build_worker_input_contract(
            self.root, plan_sha256=self.sha, video_id=job["video_id"],
            run_id=self.run_id, exact_model_id=DEFAULT_FLASH_MODEL,
            lease_token=job["lease_token"], lease_expires_utc=job["lease_expires_utc"],
            attempt=1, owner="phase5f4-owner",
        )
        materialize_and_record_nine_stages(
            self.root, contract, now_utc=self.t0 + timedelta(seconds=1),
        )
        return contract

    def _commit(self, contract, *, fault=None):
        return stage_phase5f_receipt(
            self.root, contract,
            now_utc=self.t0 + timedelta(seconds=12),
            fault_after=fault,
        )

    def _scan(self):
        return inspect_phase5f_offline_store(
            self.root, self.sha, video_id=self.video_id, run_id=self.run_id,
        )

    def _store(self):
        return (
            self.root / "data" / "videos" / self.video_id
            / "runs" / self.run_id / ".phase5f1"
        )

    def _objects(self):
        folder = self._store() / "objects"
        return _safe_directory(self.root, self.video_id, folder)

    def test_unclaimed_store_absent_and_token_free(self):
        result = self._scan()
        self.assertEqual(result["action_code"], "STORE_ABSENT")
        self.assertEqual(result["object_count"], 0)
        self.assertFalse(result["committed_receipt"])
        self.assertFalse(result["auto_cleanup_enabled"])
        self.assertFalse(result["credentials_exposed"])
        self.assertNotIn("lease_token", json.dumps(result))

    def test_uncommitted_crash_reports_orphans_without_deletion(self):
        contract = self._claim()
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_INJECTED_CRASH_AFTER_OBJECTS"
        ):
            self._commit(contract, fault="objects")
        before = {p.name for p in self._objects().iterdir()}
        first = self._scan()
        second = self._scan()
        self.assertEqual(first, second)
        self.assertEqual(first["action_code"], "UNCOMMITTED_OBJECTS")
        self.assertGreater(first["unreferenced_object_count"], 0)
        self.assertFalse(first["committed_receipt"])
        self.assertEqual({p.name for p in self._objects().iterdir()}, before)

    def test_committed_receipt_reports_exact_references(self):
        contract = self._claim()
        self._commit(contract)
        result = self._scan()
        self.assertEqual(result["action_code"], "COMMITTED_RECEIPT_VERIFIED")
        self.assertTrue(result["receipt_verified"])
        self.assertTrue(result["committed_receipt"])
        self.assertGreater(result["object_count"], 0)
        self.assertEqual(result["unreferenced_object_count"], 0)
        self.assertEqual(result["unreferenced_manifest_count"], 0)

    def test_committed_receipt_counts_later_orphan_without_deletion(self):
        contract = self._claim()
        self._commit(contract)
        extra = b"orphan-synthetic-only-not-a-model-output"
        digest, _ = _atomic_content_object(
            self.root, self.video_id, self._objects(), data=extra,
        )
        result = self._scan()
        self.assertEqual(result["action_code"], "COMMITTED_RECEIPT_VERIFIED")
        self.assertEqual(result["unreferenced_object_count"], 1)
        self.assertTrue((self._objects() / digest).is_file())

    def test_temp_leftover_is_counted_but_never_removed(self):
        contract = self._claim()
        self._commit(contract)
        temp = self._objects() / ".writing-leftover.tmp"
        temp.write_bytes(b"stale synthetic temporary bytes")
        result = self._scan()
        self.assertEqual(result["action_code"], "COMMITTED_RECEIPT_VERIFIED")
        self.assertEqual(result["temporary_file_count"], 1)
        self.assertTrue(temp.exists())

    def test_corrupted_committed_object_blocks_recovery(self):
        contract = self._claim()
        receipt = self._commit(contract)
        manifest = json.loads(
            (self._store() / "manifests" / receipt["receipt_sha256"])
            .read_text(encoding="utf-8")
        )
        target = self._objects() / manifest["artifacts"][0]["sha256"]
        target.write_bytes(b"synthetic-corruption")
        result = self._scan()
        self.assertEqual(result["action_code"], "COMMITTED_RECEIPT_CORRUPT")
        self.assertFalse(result["receipt_verified"])

    def test_existing_digest_name_with_wrong_bytes_never_overwritten(self):
        folder = self._objects()
        payload = b"correct-synthetic-content"
        digest = hashlib.sha256(payload).hexdigest()
        target = folder / digest
        target.write_bytes(b"wrong-content")
        with self.assertRaisesRegex(
            Phase5FReceiptError, "PHASE5F_RECEIPT_OBJECT_CORRUPTED"
        ):
            _atomic_content_object(
                self.root, self.video_id, folder, data=payload,
            )
        self.assertEqual(target.read_bytes(), b"wrong-content")
        self.assertFalse(any(p.name.startswith(".writing-") for p in folder.iterdir()))

    def test_atomic_link_failure_has_no_fallback_to_overwrite(self):
        folder = self._objects()
        with patch("ytresearch.phase5f_staging.os.link",
                   side_effect=OSError("no hardlink")):
            with self.assertRaisesRegex(
                Phase5FReceiptError, "PHASE5F_RECEIPT_ATOMIC_LINK_UNSUPPORTED"
            ):
                _atomic_content_object(
                    self.root, self.video_id, folder, data=b"fake bytes",
                )
        self.assertEqual(list(folder.iterdir()), [])

    def test_competing_identical_writes_are_non_destructive(self):
        folder = self._objects()
        payload = b"synthetic-parallel-content"
        def write(_):
            return _atomic_content_object(
                self.root, self.video_id, folder, data=payload,
            )
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(write, range(8)))
        self.assertEqual(len({s for s, _ in results}), 1)
        self.assertEqual(len(list(folder.iterdir())), 1)
        self.assertFalse(any(x.name.startswith(".writing-") for x in folder.iterdir()))

    def test_symlink_store_is_rejected_without_traversal(self):
        self._claim()
        store = self._store()
        store.parent.mkdir(parents=True, exist_ok=True)
        external = self.root / "unrelated-outside-store"
        external.mkdir()
        try:
            store.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable in runner")
        self.assertEqual(self._scan()["action_code"], "STORE_UNSAFE")
        self.assertEqual(list(external.iterdir()), [])

    def test_unknown_file_in_object_directory_blocks_audit(self):
        contract = self._claim()
        self._commit(contract)
        unexpected = self._objects() / "unknown-file"
        unexpected.write_text("test", encoding="utf-8")
        result = self._scan()
        self.assertEqual(result["action_code"], "STORE_UNSAFE")
        self.assertEqual(result["unexpected_file_count"], 1)
        self.assertTrue(unexpected.exists())

    def test_invalid_run_id_rejected_without_filesystem_changes(self):
        with self.assertRaisesRegex(Exception, "PHASE5F_RECOVERY_RUN_INVALID"):
            inspect_phase5f_offline_store(
                self.root, self.sha, video_id=self.video_id, run_id="../invalid",
            )
        self.assertFalse(self._store().exists())


if __name__ == "__main__":
    unittest.main()
