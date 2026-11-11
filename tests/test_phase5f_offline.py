"""Phase 5F synthetic adapter integration: no models or source downloads."""
from datetime import timedelta
import json
from pathlib import Path
import socket
import tempfile
import unittest

from ytresearch.batch_budget import batch_budget_status
from ytresearch.batch_queue import BatchQueueError, batch_queue_status
from ytresearch.batch_worker_contract import (
    DEFAULT_FLASH_MODEL, WorkerContractError,
    create_phase5e_isolated_workspace,
)
from ytresearch.phase5f_offline import (
    OFFLINE_ADAPTER_MODE, Phase5FOfflineError,
    SyntheticNineStageAdapter, run_phase5f_offline_next,
)


class Phase5FOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p5f_fixture_")
        self.addCleanup(self.tmp.cleanup)
        ws = create_phase5e_isolated_workspace(Path(self.tmp.name))
        self.root, self.sha, self.t0 = ws["root"], ws["plan_sha256"], ws["t0"]
        self.index_bytes = ws["index_bytes"]

    def run_next(self, adapter=None):
        return run_phase5f_offline_next(
            self.root, self.sha, owner="p5f-owner",
            run_id="run_p5f_fixture_01",
            exact_model_id=DEFAULT_FLASH_MODEL,
            adapter=adapter, now_utc=self.t0,
        )

    def test_end_to_end_verified_synthetic_artifacts_and_settlement(self):
        result = self.run_next()
        self.assertEqual(result["status"], "SIMULATED")
        self.assertEqual(result["mode"], OFFLINE_ADAPTER_MODE)
        self.assertTrue(result["external_calls_blocked"])
        self.assertFalse(result["private_data_published"])
        self.assertEqual(result["actual_agy_calls"], 0)
        self.assertEqual(result["actual_youtube_calls"], 0)
        self.assertEqual((self.root / "research" / "index.json").read_bytes(), self.index_bytes)
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"].get("SIMULATED"), 1)
        self.assertNotIn("lease_token", json.dumps(result))

    def test_rate_limit_global_pause_keeps_reservation(self):
        result = self.run_next(SyntheticNineStageAdapter(simulate_rate_limit=True))
        self.assertEqual(result["status"], "PAUSED_FAILURE")
        self.assertTrue(batch_budget_status(self.root, self.sha)["rate_limit"]["global_paused"])
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"].get("PAUSED_FAILURE"), 1)
        self.assertGreater(batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 0)
        with self.assertRaises((BatchQueueError, WorkerContractError, ValueError)):
            self.run_next()

    def test_live_adapter_rejected_before_queue_claim(self):
        class LiveAdapter:
            execution_mode = "LIVE"
            def execute(self, root, contract, *, now_utc):
                raise AssertionError("should not run")
        with self.assertRaisesRegex(Phase5FOfflineError, "PHASE5F_LIVE_ADAPTER_FORBIDDEN"):
            self.run_next(LiveAdapter())
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"], {"PENDING": 3})

    def test_real_media_tamper_rejected_before_claim(self):
        video = self.root / "data" / "videos" / f"{1:011d}" / "media" / "video.mp4"
        video.write_bytes(b"not-a-synthetic-fixture")
        with self.assertRaisesRegex(Phase5FOfflineError, "PHASE5F_SYNTHETIC_FIXTURE_REQUIRED"):
            self.run_next()
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"], {"PENDING": 3})

    def test_socket_attempt_blocked_and_lease_retained(self):
        class NetworkAttempt:
            execution_mode = OFFLINE_ADAPTER_MODE
            def execute(self, root, contract, *, now_utc):
                socket.create_connection(("example.com", 443), timeout=0.01)
        with self.assertRaisesRegex(WorkerContractError, "UNAUTHORIZED_EXTERNAL_SOCKET_CALL"):
            self.run_next(NetworkAttempt())
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"].get("LEASED"), 1)

    def test_missing_usage_not_treated_as_zero(self):
        class MissingUsage:
            execution_mode = OFFLINE_ADAPTER_MODE
            def execute(self, root, contract, *, now_utc):
                return {"kind": "SIMULATED_SUCCESS", "usage_source": None}
        with self.assertRaisesRegex(Phase5FOfflineError, "PHASE5F_USAGE_REQUIRED"):
            self.run_next(MissingUsage())
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"].get("LEASED"), 1)
        self.assertGreater(batch_budget_status(self.root, self.sha)["global"]["unsettled_reservations"], 0)

    def test_run_id_and_exact_model_fail_before_queue_claim(self):
        with self.assertRaisesRegex(Phase5FOfflineError, "PHASE5F_RUN_ID_INVALID"):
            run_phase5f_offline_next(self.root, self.sha, owner="w", run_id="../bad")
        with self.assertRaisesRegex(Phase5FOfflineError, "PHASE5F_EXACT_MODEL_REQUIRED"):
            run_phase5f_offline_next(self.root, self.sha, owner="w",
                                    run_id="run_p5f_fixture_01", exact_model_id="auto")
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"], {"PENDING": 3})

    def test_second_claim_is_not_a_duplicate_settlement(self):
        first = self.run_next()
        second = run_phase5f_offline_next(
            self.root, self.sha, owner="p5f-owner", run_id="run_p5f_fixture_02",
            now_utc=self.t0 + timedelta(seconds=61),
        )
        self.assertNotEqual(first["video_id"], second["video_id"])
        self.assertEqual(batch_queue_status(self.root, self.sha)["counts"].get("SIMULATED"), 2)


if __name__ == "__main__":
    unittest.main()
