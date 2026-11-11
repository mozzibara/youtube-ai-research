"""Runnable Phase 5D multi-thread contention & 429 recovery stress test."""
from __future__ import annotations
import unittest

from ytresearch.batch_stress import run_phase5d_synthetic_stress


class SyntheticStressTests(unittest.TestCase):
    def test_twelve_contending_workers_replay_and_rate_stop(self):
        result = run_phase5d_synthetic_stress(workers=12, rounds=2)
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["claim_attempts"], 24)
        self.assertEqual(result["leases_granted_under_contention"], 2)
        self.assertEqual(result["leases_denied_by_lock"], 22)
        self.assertEqual(result["checkpoint_commit_crashes"], 2)
        self.assertEqual(result["checkpoint_idempotent_replays"], 2)
        self.assertEqual(result["simulated_429_global_pauses"], 2)
        self.assertEqual(result["blocked_global_claims"], 2)
        self.assertEqual(result["operator_reconciliations"], 2)
        self.assertEqual(result["simulated_jobs"], 4)
        self.assertEqual(result["actual_agy_calls"], 0)
        self.assertEqual(result["violations"], 0)

    def test_stress_bounds_fail_without_side_effects(self):
        for workers, rounds in ((0,1),(1,1),(33,1),(12,0),(12,11)):
            with self.subTest(workers=workers,rounds=rounds):
                with self.assertRaisesRegex(ValueError,"STRESS_.*_OUT_OF_RANGE"):
                    run_phase5d_synthetic_stress(workers=workers,rounds=rounds)


if __name__ == "__main__":
    unittest.main()
