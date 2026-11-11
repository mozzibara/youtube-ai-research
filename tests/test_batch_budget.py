"""Phase 5C: model budgets, atomic lease reservation, global 429, no AGY."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ytresearch.batch_budget import (
    BatchBudgetError, batch_budget_status, configure_batch_budget,
    mark_global_rate_limited, resume_global_rate_limit,
    reconcile_paused_reservation,
)
from ytresearch.batch_plan import plan_research_batch
from ytresearch.batch_queue import (
    BatchQueueError, initialize_batch_queue, claim_batch_job,
    finish_batch_job, recover_batch_job, batch_queue_status,
)


class BatchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "research").mkdir()
        self.index = self.root / "research" / "index.json"
        self.index.write_text(json.dumps({
            "source_count": 77,
            "sources": [
                {"source_id": f"S{i:03d}", "source_ids": [f"S{i:03d}"],
                 "video_id": f"{i:011d}", "playlist_position": i}
                for i in range(1, 78)
            ],
        }), encoding="utf-8")
        for i in range(1, 5):
            vid = f"{i:011d}"
            folder = self.root / "data" / "videos" / vid
            folder.mkdir(parents=True, exist_ok=True)
            v = folder / "video.mp4"
            c = folder / "captions.json3"
            v.write_bytes(b"not real video")
            c.write_text("{}", encoding="utf-8")
            (folder / "media_cache.json").write_text(json.dumps({
                "assets": {"video":{"cached_path":str(v)},
                           "subtitle_json3":{"cached_path":str(c)}}
            }), encoding="utf-8")
        self.plan = plan_research_batch(self.root, max_jobs=3, save=True)
        self.sha = self.plan["plan_sha256"]
        initialize_batch_queue(self.root,self.sha)
        self.t0 = datetime(2026,11,11,1,0,0,tzinfo=timezone.utc)
        self.policy = {
            "schema_version":1,
            "mode":"SYNTHETIC_DRYRUN_ONLY",
            "budget_id":"pilot-2026-11",
            "global":{"max_tokens":80000,"max_api_equivalent_usd":"0.30"},
            "models":{
                "gemini-3.8-flash-medium":{
                    "max_tokens":50000,"max_api_equivalent_usd":"0.10",
                    "reserve":{
                        "uncached_input_tokens":10000,
                        "cache_read_tokens":0,"output_tokens":1000,
                    },
                },
                "gemini-4-argon-high":{
                    "max_tokens":40000,"max_api_equivalent_usd":"0.20",
                    "reserve":{
                        "uncached_input_tokens":5000,
                        "cache_read_tokens":0,"output_tokens":1000,
                    },
                },
            },
            "rate_limit":{"max_requests_per_window":2,"window_seconds":60},
        }
        self.cfgfile = self.root / "policy.json"
        self._save_policy()
        self.model_f = "gemini-3.8-flash-medium"
        self.model_a = "gemini-4-argon-high"
        self.u_f = {
            "uncached_input_tokens":3000,
            "cache_read_tokens":0,
            "output_tokens":500,
        }

    def _save_policy(self):
        self.cfgfile.write_text(json.dumps(self.policy), encoding="utf-8")

    def _configure(self):
        return configure_batch_budget(self.root,self.sha,self.cfgfile,now_utc=self.t0)

    def _claim(self, owner="synthetic", model=None, when=None):
        return claim_batch_job(self.root,self.sha,owner=owner,
                               model=self.model_f if model is None else model,
                               now_utc=when or self.t0)

    def _finish(self, claim, *, usage=None, success=True, when=None):
        return finish_batch_job(
            self.root,self.sha,video_id=claim["video_id"],
            lease_token=claim["lease_token"],success=success,
            usage=self.u_f if usage is None else usage,
            now_utc=when or self.t0+timedelta(seconds=1),
        )

    def test_config_pins_prices_and_stays_dryrun(self):
        before=self.index.read_bytes()
        status=self._configure()
        self.assertEqual(status["budget_id"],"pilot-2026-11")
        self.assertEqual(status["global"]["used_and_reserved_tokens"],0)
        self.assertFalse(status["actual_agy_invoice_observed"])
        self.assertFalse(status["live_model_calls_enabled"])
        self.assertEqual(status["pricing"]["billing_interpretation"],
                         "API_EQUIVALENT_ESTIMATE_NOT_AGY_BILL")
        self.assertEqual(self._configure(),status)
        self.assertEqual(before,self.index.read_bytes())
        self.assertFalse((self.root/"data"/"research_state.sqlite3").exists())
        self.assertFalse((self.root/"data"/"videos"/f"{1:011d}"/"runs").exists())

    def test_atomic_reserve_then_settle_no_double_count(self):
        self._configure()
        first=self._claim()
        self.assertEqual(first["budget_reservation"]["model"],self.model_f)
        self.assertEqual(first["budget_reservation"]["reservation_tokens"],11000)
        self.assertEqual(first["budget_reservation"]["reservation_api_equivalent_usd"],"0.011250")
        self.assertFalse(first["network_or_model_execution"])
        status=batch_budget_status(self.root,self.sha,now_utc=self.t0)
        self.assertEqual(status["global"]["used_and_reserved_tokens"],11000)
        self.assertEqual(status["global"]["unsettled_reservations"],1)
        finished=self._finish(first)
        self.assertEqual(finished["status"],"SIMULATED")
        self.assertEqual(finished["budget_accounting"]["reported_tokens"],3500)
        self.assertEqual(finished["budget_accounting"]["estimated_api_equivalent_usd"],"0.004125")
        status=batch_budget_status(self.root,self.sha,now_utc=self.t0)
        self.assertEqual(status["global"]["used_and_reserved_tokens"],3500)
        self.assertEqual(status["global"]["unsettled_reservations"],0)
        self.assertEqual(status["models"][self.model_f]["used_and_reserved_api_equivalent_usd"],"0.004125")
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_LEASE_TOKEN_OR_STATUS_INVALID"):
            self._finish(first)
        self.assertEqual(batch_budget_status(self.root,self.sha,now_utc=self.t0)["global"]["used_and_reserved_tokens"],3500)

    def test_configured_model_mandatory_unknown_model_rejected_without_lease(self):
        self._configure()
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_EXACT_MODEL_REQUIRED"):
            claim_batch_job(self.root,self.sha,owner="worker",now_utc=self.t0)
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_EXACT_MODEL_REQUIRED"):
            claim_batch_job(self.root,self.sha,owner="worker",model="gemini-flash-unknown",
                            now_utc=self.t0)
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],{"PENDING":3})
        self.assertEqual(batch_budget_status(self.root,self.sha,now_utc=self.t0)["global"]["used_and_reserved_tokens"],0)

    def test_missing_or_over_budget_usage_does_not_release_lease(self):
        self._configure()
        first=self._claim()
        for usage,err in ((None,"BUDGET_USAGE_REQUIRED_BEFORE_FINISH"),(
            {"uncached_input_tokens":11000,"cache_read_tokens":0,"output_tokens":1000},
            "BUDGET_ACTUAL_EXCEEDS_RESERVATION")):
            with self.subTest(err=err),self.assertRaisesRegex(BatchBudgetError,err):
                finish_batch_job(self.root,self.sha,video_id=first["video_id"],
                                 lease_token=first["lease_token"],success=True,
                                 usage=usage,now_utc=self.t0+timedelta(seconds=1))
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],{"LEASED":1,"PENDING":2})
        self.assertEqual(batch_budget_status(self.root,self.sha,now_utc=self.t0)["global"]["unsettled_reservations"],1)
        self._finish(first)

    def test_exact_model_and_global_caps_enforced_without_spending(self):
        self.policy["models"][self.model_f]["max_tokens"]=13000
        self._save_policy()
        self._configure()
        first=self._claim()
        self._finish(first,usage={
            "uncached_input_tokens":8000,"cache_read_tokens":0,"output_tokens":500
        })
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_MODEL_CAP_EXCEEDED"):
            self._claim(when=self.t0+timedelta(seconds=10))
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],{"PENDING":2,"SIMULATED":1})
        self.assertEqual(batch_budget_status(self.root,self.sha,now_utc=self.t0)["global"]["used_and_reserved_tokens"],8500)

    def test_global_budget_cap_enforced_separately(self):
        self.policy["global"]["max_tokens"]=15000
        self._save_policy()
        self._configure()
        first=self._claim()
        self._finish(first,usage={"uncached_input_tokens":9000,"cache_read_tokens":0,"output_tokens":0})
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_GLOBAL_CAP_EXCEEDED"):
            self._claim(when=self.t0+timedelta(seconds=10))

    def test_global_rate_window_across_models_and_no_automatic_retries(self):
        self._configure()
        a=self._claim(when=self.t0)
        self._finish(a,when=self.t0+timedelta(seconds=1))
        b=self._claim(model=self.model_a,when=self.t0+timedelta(seconds=2))
        self._finish(b,usage={"uncached_input_tokens":1000,"cache_read_tokens":0,"output_tokens":100},
                     when=self.t0+timedelta(seconds=3))
        with self.assertRaisesRegex(BatchBudgetError,"GLOBAL_RATE_WINDOW_LIMIT"):
            self._claim(when=self.t0+timedelta(seconds=4))
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],{"PENDING":1,"SIMULATED":2})
        self.assertEqual(batch_budget_status(self.root,self.sha,now_utc=self.t0+timedelta(seconds=4))["rate_limit"]["requests_in_window"],2)
        c=self._claim(when=self.t0+timedelta(seconds=70))
        self.assertEqual(c["video_id"],f"{3:011d}")

    def test_429_persists_global_stop_manual_resume_after_cooldown(self):
        self._configure()
        first=self._claim()
        self._finish(first)
        paused=mark_global_rate_limited(
            self.root,self.sha,error_code="429",actor="operator",
            reason="Received a 429 Resource Exhausted response from a simulated provider.",
            retry_after_seconds=90,now_utc=self.t0+timedelta(seconds=3),
        )
        self.assertTrue(paused["rate_limit"]["global_paused"])
        with self.assertRaisesRegex(BatchBudgetError,"GLOBAL_RATE_LIMIT_PAUSED"):
            self._claim(when=self.t0+timedelta(seconds=95))
        with self.assertRaisesRegex(BatchBudgetError,"GLOBAL_RATE_COOLDOWN_ACTIVE"):
            resume_global_rate_limit(
                self.root,self.sha,actor="operator",
                reason="Manually inspected quota and confirmed recovery readiness.",
                now_utc=self.t0+timedelta(seconds=20),
            )
        resumed=resume_global_rate_limit(
            self.root,self.sha,actor="operator",
            reason="Manually inspected the simulated provider and cleared stop.",
            now_utc=self.t0+timedelta(seconds=95),
        )
        self.assertFalse(resumed["rate_limit"]["global_paused"])
        second=self._claim(when=self.t0+timedelta(seconds=96))
        self.assertEqual(second["video_id"],f"{2:011d}")

    def test_429_cannot_be_shortened_and_other_errors_do_not_fake_rate_limit(self):
        self._configure()
        mark_global_rate_limited(self.root,self.sha,error_code="429",actor="admin",
            reason="Provider returned explicit quota exhausted for the simulated run.",
            retry_after_seconds=600,now_utc=self.t0)
        newer=mark_global_rate_limited(self.root,self.sha,error_code="RESOURCE_EXHAUSTED",
            actor="admin",reason="Repeated quota issue requires additional manual review.",
            retry_after_seconds=30,now_utc=self.t0+timedelta(seconds=20))
        self.assertEqual(newer["rate_limit"]["retry_not_before_utc"],
                         (self.t0+timedelta(seconds=600)).isoformat(timespec="microseconds"))
        with self.assertRaisesRegex(BatchBudgetError,"GLOBAL_RATE_ERROR_CODE_UNSUPPORTED"):
            mark_global_rate_limited(self.root,self.sha,error_code="INTERNAL_SERVER_ERROR",
                actor="admin",reason="An unrelated server error must not be misclassified.",
                now_utc=self.t0)

    def test_stale_pricing_snapshot_does_not_reprice_prior_reservations(self):
        first=self._configure()
        price_version=first["pricing"]["pricing_version"]
        folder=self.root/"config"
        folder.mkdir()
        (folder/"pricing.json").write_text(json.dumps({
            "schema_version":1,"pricing_version":"new-unverified",
            "currency":"USD","unit_tokens":1000000,
            "models":{self.model_f:{
                "input_per_1m_usd":1000,"output_per_1m_usd":1000,
                "cached_input_per_1m_usd":1000,
            }},
        }),encoding="utf-8")
        claim=self._claim()
        result=self._finish(claim)
        self.assertEqual(result["budget_accounting"]["estimated_api_equivalent_usd"],"0.004125")
        self.assertEqual(batch_budget_status(self.root,self.sha,now_utc=self.t0)["pricing"]["pricing_version"],
                         price_version)

    def test_expired_reservation_retained_until_manual_reconciliation(self):
        self._configure()
        first=self._claim()
        late=self.t0+timedelta(minutes=31)
        recover_batch_job(
            self.root,self.sha,video_id=first["video_id"],
            actor="human",reason="The expired synthetic lease requires manual recovery.",
            now_utc=late,
        )
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_UNKNOWN_USAGE_MUST_BE_RECONCILED"):
            recover_batch_job(
                self.root,self.sha,video_id=first["video_id"],retry=True,
                actor="human",reason="The synthetic attempt must be reconciled first.",
                now_utc=late,
            )
        self.assertEqual(batch_budget_status(self.root,self.sha,now_utc=late)["global"]["unsettled_reservations"],1)
        value=reconcile_paused_reservation(
            self.root,self.sha,first["video_id"],actor="human",
            reason="The local synthetic example is confirmed to use no model tokens.",
            usage={"uncached_input_tokens":0,"cache_read_tokens":0,"output_tokens":0},
            now_utc=late,
        )
        self.assertEqual(value["reported_tokens"],0)
        recover_batch_job(
            self.root,self.sha,video_id=first["video_id"],retry=True,
            actor="human",reason="The synthetic attempt is reconciled and may retry.",
            now_utc=late,
        )
        second=self._claim(when=late)
        self.assertEqual(second["video_id"],first["video_id"])

    def test_policy_validation_and_retrofit_fail_closed(self):
        self.policy["models"][self.model_f]["reserve"]["uncached_input_tokens"]=-1
        self._save_policy()
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_INVALID_INTEGER"):
            self._configure()
        self.policy["models"][self.model_f]["reserve"]["uncached_input_tokens"]=10000
        self.policy["mode"]="LIVE"
        self._save_policy()
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_POLICY_LIVE_MODE_FORBIDDEN"):
            self._configure()
        self.policy["mode"]="SYNTHETIC_DRYRUN_ONLY"
        self._save_policy()
        self._configure()
        c=self._claim()
        self.policy["models"][self.model_f]["max_tokens"]=40000
        self._save_policy()
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_POLICY_ALREADY_CONFIGURED"):
            self._configure()
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],{"LEASED":1,"PENDING":2})

    def test_legacy_simulation_stays_model_free_without_policy(self):
        claim=claim_batch_job(self.root,self.sha,owner="legacy",now_utc=self.t0)
        self.assertIsNone(claim["budget_reservation"])
        result=finish_batch_job(self.root,self.sha,video_id=claim["video_id"],
                                lease_token=claim["lease_token"],success=True,
                                now_utc=self.t0+timedelta(seconds=1))
        self.assertIsNone(result["budget_accounting"])
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_POLICY_REQUIRED_FOR_MODEL"):
            claim_batch_job(self.root,self.sha,owner="legacy",model=self.model_f,
                            now_utc=self.t0+timedelta(seconds=2))


    def test_simulated_429_finish_atomically_blocks_other_queue_jobs(self):
        self._configure()
        first=self._claim()
        failed=finish_batch_job(
            self.root,self.sha,video_id=first["video_id"],
            lease_token=first["lease_token"],success=False,
            usage=None,rate_error_code="RESOURCE_EXHAUSTED",
            retry_after_seconds=120,now_utc=self.t0+timedelta(seconds=2),
        )
        self.assertEqual(failed["status"],"PAUSED_FAILURE")
        self.assertTrue(failed["global_rate_paused"])
        self.assertIsNone(failed["budget_accounting"])
        status=batch_budget_status(self.root,self.sha,now_utc=self.t0+timedelta(seconds=2))
        self.assertTrue(status["rate_limit"]["global_paused"])
        self.assertEqual(status["global"]["unsettled_reservations"],1)
        with self.assertRaisesRegex(BatchBudgetError,"GLOBAL_RATE_UNSETTLED_USAGE_REQUIRES_RECONCILIATION"):
            resume_global_rate_limit(
                self.root,self.sha,actor="reviewer",
                reason="A simulated 429 still has unknown usage and cannot be resumed.",
                now_utc=self.t0+timedelta(seconds=200),
            )
        with self.assertRaisesRegex(BatchBudgetError,"GLOBAL_RATE_LIMIT_PAUSED"):
            self._claim(when=self.t0+timedelta(seconds=200))
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_UNKNOWN_USAGE_MUST_BE_RECONCILED"):
            recover_batch_job(
                self.root,self.sha,video_id=first["video_id"],retry=True,
                actor="reviewer",reason="Manually reviewing the synthetic rate failure.",
                now_utc=self.t0+timedelta(seconds=201),
            )
        reconcile_paused_reservation(
            self.root,self.sha,first["video_id"],
            actor="reviewer",
            reason="The simulated failed call is confirmed to have zero tokens.",
            usage={"uncached_input_tokens":0,"cache_read_tokens":0,"output_tokens":0},
            now_utc=self.t0+timedelta(seconds=202),
        )
        resume_global_rate_limit(
            self.root,self.sha,actor="reviewer",
            reason="Manual quota inspection cleared the synthetic global stop.",
            now_utc=self.t0+timedelta(seconds=202),
        )
        second=self._claim(when=self.t0+timedelta(seconds=203))
        self.assertEqual(second["video_id"],f"{2:011d}")

    def test_simulated_rate_error_with_measured_usage_settles_then_pauses(self):
        self._configure()
        first=self._claim()
        failed=finish_batch_job(
            self.root,self.sha,video_id=first["video_id"],
            lease_token=first["lease_token"],success=False,
            usage=self.u_f,rate_error_code="429",
            retry_after_seconds=15,now_utc=self.t0+timedelta(seconds=1),
        )
        self.assertEqual(failed["budget_accounting"]["reported_tokens"],3500)
        self.assertEqual(batch_budget_status(self.root,self.sha,now_utc=self.t0+timedelta(seconds=1))["global"]["unsettled_reservations"],0)
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],{
            "PAUSED_FAILURE":1,"PENDING":2,
        })

    def test_rate_failure_requires_a_budget_policy_and_failure_status(self):
        legacy=claim_batch_job(self.root,self.sha,owner="worker",now_utc=self.t0)
        with self.assertRaisesRegex(BatchBudgetError,"BUDGET_POLICY_REQUIRED_FOR_RATE_EVENT"):
            finish_batch_job(
                self.root,self.sha,video_id=legacy["video_id"],
                lease_token=legacy["lease_token"],success=False,
                rate_error_code="429",now_utc=self.t0+timedelta(seconds=1),
            )
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],{"LEASED":1,"PENDING":2})


if __name__ == "__main__":
    unittest.main()
