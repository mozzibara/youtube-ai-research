"""Phase 5E offline tests: worker input contract, file integrity, usage & safety (E01-E16)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from ytresearch.agent import Agy
from ytresearch.batch_budget import (
    BatchBudgetError,
    batch_budget_status,
    reconcile_paused_reservation,
    resume_global_rate_limit,
)
from ytresearch.batch_checkpoint import record_synthetic_stage
from ytresearch.batch_queue import (
    BatchQueueError,
    batch_queue_status,
    claim_batch_job,
    finish_batch_job,
    recover_batch_job,
)
from ytresearch.batch_worker_contract import (
    DEFAULT_ARGON_MODEL,
    DEFAULT_FLASH_MODEL,
    WORKER_ERROR_CATEGORIES,
    InjectedSyntheticCrash,
    WorkerContractError,
    build_stage_artifact_manifest,
    build_synthetic_stream_json_usage,
    build_worker_input_contract,
    classify_worker_error,
    create_phase5e_isolated_workspace,
    finish_worker_contract_job,
    list_worker_audit_events,
    materialize_and_record_nine_stages,
    normalize_agy_usage_contract,
    offline_external_call_guard,
    record_verified_stage_artifact,
    sanitize_audit_text,
    streaming_file_sha256,
    validate_multi_call_authorization_contract,
    validate_worker_input_contract,
    verified_stage_status,
)
from ytresearch.multimodal import MULTIMODAL_STAGES


class Phase5EWorkerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="phase5e_contract_")
        self.addCleanup(self.tmp.cleanup)
        self.guard_cm = offline_external_call_guard()
        self.guard = self.guard_cm.__enter__()
        self.addCleanup(lambda: self.guard_cm.__exit__(None, None, None))

        ws = create_phase5e_isolated_workspace(Path(self.tmp.name))
        self.root: Path = ws["root"]
        self.plan_sha: str = ws["plan_sha256"]
        self.index_bytes: bytes = ws["index_bytes"]
        self.t0: datetime = ws["t0"]
        self.video_id = f"{1:011d}"
        self.run_id = "run_phase5e_offline_01"

    def _claim_and_build_contract(
        self,
        *,
        model: str = DEFAULT_FLASH_MODEL,
        when: datetime | None = None,
        call_slots: list[dict] | None = None,
    ) -> tuple[dict, dict]:
        now = when or self.t0
        claim = claim_batch_job(
            self.root,
            self.plan_sha,
            owner="phase5e-worker",
            model=model,
            now_utc=now,
        )
        contract = build_worker_input_contract(
            self.root,
            plan_sha256=self.plan_sha,
            video_id=claim["video_id"],
            run_id=self.run_id,
            exact_model_id=model,
            lease_token=claim["lease_token"],
            lease_expires_utc=claim["lease_expires_utc"],
            attempt=1,
            call_slots=call_slots,
        )
        return claim, contract

    # -----------------------------------------------------------------------
    # E01: Valid offline worker contract end-to-end
    # -----------------------------------------------------------------------
    def test_e01_valid_offline_worker_contract_end_to_end(self) -> None:
        claim, contract = self._claim_and_build_contract()
        validated = validate_worker_input_contract(
            self.root, contract, now_utc=self.t0
        )
        self.assertTrue(validated["contract_valid"])
        self.assertFalse(validated["live_model_execution_permitted"])
        self.assertFalse(validated["external_network_allowed"])
        self.assertEqual(validated["source_ids"], ["S001"])
        self.assertNotIn(claim["lease_token"], json.dumps(validated))

        materialize_and_record_nine_stages(
            self.root, contract, now_utc=self.t0 + timedelta(seconds=1)
        )
        st_status = verified_stage_status(self.root, self.plan_sha, self.video_id)
        self.assertEqual(st_status["stages_done"], 9)
        self.assertTrue(st_status["verified_stage_complete"])
        self.assertTrue(st_status["real_artifact_contents_verified"])
        self.assertFalse(st_status["production_live_analysis_completed"])
        self.assertNotIn(claim["lease_token"], json.dumps(st_status))

        raw_stream = build_synthetic_stream_json_usage(
            model=DEFAULT_FLASH_MODEL,
            turns=((1200, 300, 180), (800, 200, 120)),
        )
        finished = finish_worker_contract_job(
            self.root,
            contract,
            success=True,
            usage_source=raw_stream,
            now_utc=self.t0 + timedelta(seconds=20),
        )
        self.assertEqual(finished["status"], "SIMULATED")
        self.assertFalse(finished["live_agy_enabled"])
        self.assertEqual(finished["normalized_usage"]["uncached_input_tokens"], 2000)
        self.assertEqual(finished["normalized_usage"]["cache_read_tokens"], 500)
        self.assertEqual(finished["normalized_usage"]["output_tokens"], 300)
        self.assertEqual(finished["normalized_usage"]["total_tokens"], 2800)
        self.assertTrue(finished["normalized_usage"]["double_counting_prevented"])
        self.assertIsNone(finished["normalized_usage"]["actual_agy_billing_usd"])
        self.assertFalse(finished["normalized_usage"]["actual_agy_invoice_observed"])

        b_status = batch_budget_status(
            self.root, self.plan_sha, now_utc=self.t0 + timedelta(seconds=20)
        )
        self.assertEqual(b_status["global"]["unsettled_reservations"], 0)
        self.assertEqual(b_status["global"]["used_and_reserved_tokens"], 2800)
        self.assertEqual(
            (self.root / "research" / "index.json").read_bytes(), self.index_bytes
        )
        self.assertEqual(self.guard.socket_calls, 0)
        self.assertEqual(self.guard.subprocess_calls, 0)
        self.assertEqual(self.guard.live_agy_calls, 0)
        self.assertEqual(self.guard.youtube_calls, 0)

    # -----------------------------------------------------------------------
    # E02: Stale plan rejected
    # -----------------------------------------------------------------------
    def test_e02_stale_plan_rejected(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        # Mutate media_cache.json of video 2 so the saved batch plan becomes stale.
        (self.root / "data" / "videos" / f"{2:011d}" / "media_cache.json").unlink()
        with self.assertRaises(WorkerContractError) as ctx:
            validate_worker_input_contract(self.root, contract, now_utc=self.t0)
        self.assertEqual(ctx.exception.category, "INPUT_UNAVAILABLE_OR_BLOCKED")
        self.assertIn("STALE", ctx.exception.code)

    # -----------------------------------------------------------------------
    # E03: Invalid lease rejected without leaking lease token
    # -----------------------------------------------------------------------
    def test_e03_invalid_lease_rejected_without_leaking_token(self) -> None:
        claim, contract = self._claim_and_build_contract()
        bad_contract = dict(contract, lease_token="f" * 48)
        with self.assertRaises(WorkerContractError) as ctx:
            validate_worker_input_contract(self.root, bad_contract, now_utc=self.t0)
        self.assertEqual(ctx.exception.category, "AUTHORIZATION_FAILURE")
        self.assertEqual(ctx.exception.code, "LEASE_TOKEN_INVALID")
        self.assertNotIn(claim["lease_token"], str(ctx.exception))
        self.assertNotIn("f" * 48, str(ctx.exception))

    # -----------------------------------------------------------------------
    # E04: Expired lease rejected and manual recovery lifecycle
    # -----------------------------------------------------------------------
    def test_e04_expired_lease_rejected_and_manual_recovery_lifecycle(self) -> None:
        claim, contract = self._claim_and_build_contract()
        expired_time = self.t0 + timedelta(minutes=31)
        with self.assertRaises(WorkerContractError) as ctx:
            validate_worker_input_contract(self.root, contract, now_utc=expired_time)
        self.assertEqual(ctx.exception.category, "LEASE_EXPIRED")
        self.assertEqual(ctx.exception.code, "LEASE_EXPIRED_RECOVERY_REQUIRED")

        # Concurrent worker cannot claim while expired lease is still LEASED.
        with self.assertRaisesRegex(BatchQueueError, "QUEUE_ACTIVE_OR_EXPIRED_LEASE"):
            claim_batch_job(
                self.root,
                self.plan_sha,
                owner="second-worker",
                model=DEFAULT_FLASH_MODEL,
                now_utc=expired_time,
            )

        # Operator explicitly recovers to PAUSED_EXPIRED, reconciles budget, then retries.
        rec = recover_batch_job(
            self.root,
            self.plan_sha,
            video_id=claim["video_id"],
            actor="operator",
            reason="Expired worker contract lease recovered manually for inspection.",
            now_utc=expired_time,
        )
        self.assertEqual(rec["status"], "PAUSED_EXPIRED")
        reconcile_paused_reservation(
            self.root,
            self.plan_sha,
            claim["video_id"],
            actor="operator",
            reason="Verified zero external model tokens were spent before lease expiry.",
            usage={"uncached_input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0},
            now_utc=expired_time,
        )
        retried = recover_batch_job(
            self.root,
            self.plan_sha,
            video_id=claim["video_id"],
            actor="operator",
            reason="Authorized second attempt after reconciling expired reservation.",
            retry=True,
            now_utc=expired_time,
        )
        self.assertEqual(retried["status"], "PENDING")

    # -----------------------------------------------------------------------
    # E05: Stage reorder rejected
    # -----------------------------------------------------------------------
    def test_e05_stage_reorder_rejected(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        vdir = self.root / "data" / "videos" / self.video_id
        in_m = build_stage_artifact_manifest(
            self.root, self.video_id, [vdir / "media_cache.json"]
        )
        parsed = vdir / "parsed_subtitles.json"
        parsed.write_text('{"segments":[]}', encoding="utf-8")
        out_m = build_stage_artifact_manifest(self.root, self.video_id, [parsed])

        with self.assertRaises(WorkerContractError) as ctx:
            record_verified_stage_artifact(
                self.root,
                contract,
                stage_name="subtitles_parsed",  # Stage 1 before Stage 0
                input_manifest=in_m,
                output_manifest=out_m,
                now_utc=self.t0,
            )
        self.assertEqual(ctx.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx.exception.code, "STAGE_OUT_OF_ORDER")
        self.assertEqual(
            verified_stage_status(self.root, self.plan_sha, self.video_id)["stages_done"],
            0,
        )

    # -----------------------------------------------------------------------
    # E06: Digest mismatch / size mismatch / missing file rejected
    # -----------------------------------------------------------------------
    def test_e06_digest_and_size_mismatch_and_missing_file_rejected(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        vdir = self.root / "data" / "videos" / self.video_id
        in_m = build_stage_artifact_manifest(
            self.root,
            self.video_id,
            [vdir / "media" / "video.mp4", vdir / "media" / "captions.json3"],
        )
        out_m = build_stage_artifact_manifest(
            self.root, self.video_id, [vdir / "media_cache.json"]
        )

        # Case 1: Tampered SHA-256 string
        forged_out = json.loads(json.dumps(out_m))
        forged_out["artifacts"][0]["sha256"] = "a" * 64
        with self.assertRaises(WorkerContractError) as ctx1:
            record_verified_stage_artifact(
                self.root,
                contract,
                stage_name="media_cached",
                input_manifest=in_m,
                output_manifest=forged_out,
                now_utc=self.t0,
            )
        self.assertEqual(ctx1.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx1.exception.code, "ARTIFACT_SHA256_MISMATCH")

        # Case 2: Tampered size_bytes
        wrong_size_out = json.loads(json.dumps(out_m))
        wrong_size_out["artifacts"][0]["size_bytes"] += 10
        with self.assertRaises(WorkerContractError) as ctx2:
            record_verified_stage_artifact(
                self.root,
                contract,
                stage_name="media_cached",
                input_manifest=in_m,
                output_manifest=wrong_size_out,
                now_utc=self.t0,
            )
        self.assertEqual(ctx2.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx2.exception.code, "ARTIFACT_SIZE_MISMATCH")

        # Case 3: Declared path + hash for non-existent file on disk
        ghost_out = {
            "schema_version": 1,
            "video_id": self.video_id,
            "artifacts": [{
                "name": "ghost.json",
                "relpath": f"data/videos/{self.video_id}/ghost.json",
                "size_bytes": 12,
                "sha256": "b" * 64,
            }],
        }
        with self.assertRaises(WorkerContractError) as ctx3:
            record_verified_stage_artifact(
                self.root,
                contract,
                stage_name="media_cached",
                input_manifest=in_m,
                output_manifest=ghost_out,
                now_utc=self.t0,
            )
        self.assertEqual(ctx3.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx3.exception.code, "ARTIFACT_FILE_MISSING")

    # -----------------------------------------------------------------------
    # E07: Modified, replaced, or deleted artifact on disk rejected
    # -----------------------------------------------------------------------
    def test_e07_modified_or_replaced_artifact_after_checkpoint_rejected(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        materialize_and_record_nine_stages(
            self.root, contract, now_utc=self.t0, up_to_stage=9
        )
        parsed_sub = self.root / "data" / "videos" / self.video_id / "parsed_subtitles.json"
        original_bytes = parsed_sub.read_bytes()

        # 1. Same-size byte modification on disk -> ARTIFACT_SHA256_MISMATCH
        flipped = bytearray(original_bytes)
        flipped[-3] = ord("X") if flipped[-3] != ord("X") else ord("Y")
        parsed_sub.write_bytes(bytes(flipped))
        with self.assertRaises(WorkerContractError) as ctx_sha:
            verified_stage_status(self.root, self.plan_sha, self.video_id)
        self.assertEqual(ctx_sha.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx_sha.exception.code, "ARTIFACT_SHA256_MISMATCH")

        # 2. Size-changing replacement on disk -> ARTIFACT_SIZE_MISMATCH
        parsed_sub.write_text('{"tampered": true}\n', encoding="utf-8")
        with self.assertRaises(WorkerContractError) as ctx_size:
            finish_worker_contract_job(
                self.root,
                contract,
                success=True,
                usage_source=build_synthetic_stream_json_usage(),
                now_utc=self.t0 + timedelta(seconds=15),
            )
        self.assertEqual(ctx_size.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx_size.exception.code, "ARTIFACT_SIZE_MISMATCH")

        # 3. Deleted file on disk -> ARTIFACT_FILE_MISSING
        parsed_sub.unlink()
        with self.assertRaises(WorkerContractError) as ctx_missing:
            verified_stage_status(self.root, self.plan_sha, self.video_id)
        self.assertEqual(ctx_missing.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx_missing.exception.code, "ARTIFACT_FILE_MISSING")

    # -----------------------------------------------------------------------
    # E08: Path traversal and outside-root path rejected
    # -----------------------------------------------------------------------
    def test_e08_path_traversal_and_outside_root_rejected(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        outside_file = self.root / "outside_secret.json"
        outside_file.write_text("{}", encoding="utf-8")
        sha, size = streaming_file_sha256(outside_file)

        for bad_path in (
            "../outside_secret.json",
            "../../research/index.json",
            str(outside_file),
        ):
            with self.subTest(bad_path=bad_path):
                bad_manifest = {
                    "schema_version": 1,
                    "video_id": self.video_id,
                    "artifacts": [{
                        "name": "outside.json",
                        "path": bad_path,
                        "size_bytes": size,
                        "sha256": sha,
                    }],
                }
                with self.assertRaises(WorkerContractError) as ctx:
                    record_verified_stage_artifact(
                        self.root,
                        contract,
                        stage_name="media_cached",
                        input_manifest=bad_manifest,
                        output_manifest=bad_manifest,
                        now_utc=self.t0,
                    )
                self.assertEqual(ctx.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
                self.assertIn(
                    ctx.exception.code,
                    ("ARTIFACT_PATH_TRAVERSAL_BLOCKED", "ARTIFACT_PATH_OUTSIDE_APPROVED_ROOT"),
                )

    # -----------------------------------------------------------------------
    # E09: Missing or incomplete usage retains reservation (never defaults to 0)
    # -----------------------------------------------------------------------
    def test_e09_missing_or_incomplete_usage_retains_reservation(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        materialize_and_record_nine_stages(self.root, contract, now_utc=self.t0)

        for bad_usage in (
            None,
            "",
            {},
            {"uncached_input_tokens": 100, "cache_read_tokens": 0},  # missing output_tokens
            {"uncached_input_tokens": "UNKNOWN", "cache_read_tokens": 0, "output_tokens": 10},
        ):
            with self.subTest(bad_usage=bad_usage):
                with self.assertRaises(WorkerContractError) as ctx:
                    finish_worker_contract_job(
                        self.root,
                        contract,
                        success=True,
                        usage_source=bad_usage,
                        now_utc=self.t0 + timedelta(seconds=15),
                    )
                self.assertEqual(ctx.exception.category, "USAGE_UNKNOWN")

        # Lease remains LEASED and worst-case reservation remains RESERVED (not 0).
        q_status = batch_queue_status(self.root, self.plan_sha)
        self.assertEqual(q_status["counts"], {"LEASED": 1, "PENDING": 2})
        b_status = batch_budget_status(self.root, self.plan_sha, now_utc=self.t0)
        self.assertEqual(b_status["global"]["unsettled_reservations"], 1)
        self.assertEqual(b_status["global"]["used_and_reserved_tokens"], 14000)

    # -----------------------------------------------------------------------
    # E10: Duplicate cumulative usage and duplicate call_id rejected
    # -----------------------------------------------------------------------
    def test_e10_duplicate_cumulative_usage_and_call_id_dedup_rejected(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        materialize_and_record_nine_stages(self.root, contract, now_utc=self.t0)

        # Case 1: Stream-JSON where result.usage double-counts cumulative + step turns
        double_counted_stream = build_synthetic_stream_json_usage(
            model=DEFAULT_FLASH_MODEL,
            turns=((1000, 200, 100), (500, 100, 50)),
            cumulative_override=(3000, 600, 300),  # 2x the turn sum!
        )
        with self.assertRaises(WorkerContractError) as ctx1:
            normalize_agy_usage_contract(
                double_counted_stream,
                expected_model_id=DEFAULT_FLASH_MODEL,
                plan_sha256=self.plan_sha,
                video_id=self.video_id,
                run_id=self.run_id,
                attempt=1,
            )
        self.assertEqual(ctx1.exception.category, "USAGE_UNKNOWN")
        self.assertIn("CUMULATIVE", ctx1.exception.code)

        # Case 2: Structured records mixing turn and cumulative with sum_all_records=True
        mixed_records = {
            "exact_model_id": DEFAULT_FLASH_MODEL,
            "sum_all_records": True,
            "records": [
                {"record_id": "t1", "scope": "turn", "uncached_input_tokens": 500, "cache_read_tokens": 0, "output_tokens": 50},
                {"record_id": "t2", "scope": "turn", "uncached_input_tokens": 500, "cache_read_tokens": 0, "output_tokens": 50},
                {"record_id": "cum", "scope": "cumulative", "uncached_input_tokens": 1000, "cache_read_tokens": 0, "output_tokens": 100},
            ],
        }
        with self.assertRaises(WorkerContractError) as ctx2:
            normalize_agy_usage_contract(
                mixed_records,
                expected_model_id=DEFAULT_FLASH_MODEL,
                plan_sha256=self.plan_sha,
                video_id=self.video_id,
                run_id=self.run_id,
                attempt=1,
            )
        self.assertEqual(ctx2.exception.category, "USAGE_UNKNOWN")
        self.assertEqual(ctx2.exception.code, "USAGE_DUPLICATE_CUMULATIVE_REJECTED")

        # Case 3: Duplicate step_id inside stream-json
        dup_step_events = [
            {"event": "init", "init": {"model": DEFAULT_FLASH_MODEL}},
            {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "usage": {"input_tokens": 100, "cache_read_tokens": 0, "output_tokens": 10}}},
            {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "usage": {"input_tokens": 100, "cache_read_tokens": 0, "output_tokens": 10}}},
        ]
        with self.assertRaises(WorkerContractError) as ctx3:
            normalize_agy_usage_contract(
                dup_step_events,
                expected_model_id=DEFAULT_FLASH_MODEL,
                plan_sha256=self.plan_sha,
                video_id=self.video_id,
                run_id=self.run_id,
                attempt=1,
            )
        self.assertEqual(ctx3.exception.category, "USAGE_UNKNOWN")
        self.assertEqual(ctx3.exception.code, "USAGE_DUPLICATE_STEP_ID")

    # -----------------------------------------------------------------------
    # E11: Over-budget usage rejected and rolls back settlement
    # -----------------------------------------------------------------------
    def test_e11_over_budget_usage_rejected_and_rolls_back(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        materialize_and_record_nine_stages(self.root, contract, now_utc=self.t0)

        # Reserved uncached_input_tokens for Flash is 10000; supply 12000.
        over_budget_stream = build_synthetic_stream_json_usage(
            model=DEFAULT_FLASH_MODEL,
            turns=((7000, 100, 100), (5000, 100, 100)),
        )
        with self.assertRaises(WorkerContractError) as ctx:
            finish_worker_contract_job(
                self.root,
                contract,
                success=True,
                usage_source=over_budget_stream,
                now_utc=self.t0 + timedelta(seconds=15),
            )
        self.assertEqual(ctx.exception.category, "CONTRACT_VIOLATION")
        self.assertEqual(ctx.exception.code, "BUDGET_ACTUAL_EXCEEDS_RESERVATION")

        self.assertEqual(
            batch_queue_status(self.root, self.plan_sha)["counts"],
            {"LEASED": 1, "PENDING": 2},
        )
        self.assertEqual(
            batch_budget_status(self.root, self.plan_sha, now_utc=self.t0)["global"]["unsettled_reservations"],
            1,
        )

    # -----------------------------------------------------------------------
    # E12: 429 global pause vs transient provider error isolation
    # -----------------------------------------------------------------------
    def test_e12_429_global_pause_and_transient_error_separation(self) -> None:
        # 1. Transient error on video 1 pauses ONLY video 1, NOT global rate limit.
        claim1, contract1 = self._claim_and_build_contract()
        fail_transient = finish_worker_contract_job(
            self.root,
            contract1,
            success=False,
            provider_error_code="503_SERVICE_UNAVAILABLE",
            usage_source={"uncached_input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0},
            now_utc=self.t0 + timedelta(seconds=2),
        )
        self.assertEqual(fail_transient["status"], "PAUSED_FAILURE")
        self.assertEqual(fail_transient["error_category"], "TRANSIENT_PROVIDER_FAILURE")
        self.assertFalse(fail_transient["global_rate_paused"])

        # 2. 429 on video 2 pauses globally and retains unknown reservation.
        claim2 = claim_batch_job(
            self.root,
            self.plan_sha,
            owner="phase5e-worker-2",
            model=DEFAULT_FLASH_MODEL,
            now_utc=self.t0 + timedelta(seconds=5),
        )
        contract2 = build_worker_input_contract(
            self.root,
            plan_sha256=self.plan_sha,
            video_id=claim2["video_id"],
            run_id="run_phase5e_02",
            exact_model_id=DEFAULT_FLASH_MODEL,
            lease_token=claim2["lease_token"],
            lease_expires_utc=claim2["lease_expires_utc"],
            attempt=1,
        )
        fail_429 = finish_worker_contract_job(
            self.root,
            contract2,
            success=False,
            provider_error_code="429",
            retry_after_seconds=120,
            usage_source=None,
            now_utc=self.t0 + timedelta(seconds=6),
        )
        self.assertEqual(fail_429["status"], "PAUSED_FAILURE")
        self.assertEqual(fail_429["error_category"], "GLOBAL_RATE_LIMIT")
        self.assertTrue(fail_429["global_rate_paused"])
        self.assertTrue(fail_429["reservation_retained"])

        b_status = batch_budget_status(
            self.root, self.plan_sha, now_utc=self.t0 + timedelta(seconds=6)
        )
        self.assertTrue(b_status["rate_limit"]["global_paused"])
        self.assertEqual(b_status["global"]["unsettled_reservations"], 1)

        # Audit log has only sanitized codes, never lease tokens or raw messages.
        audits = list_worker_audit_events(self.root, self.plan_sha)
        self.assertEqual(len(audits), 2)
        self.assertNotIn(claim1["lease_token"], json.dumps(audits))
        self.assertNotIn(claim2["lease_token"], json.dumps(audits))

    # -----------------------------------------------------------------------
    # E13: Early resume rejected before cooldown and manual reconciliation
    # -----------------------------------------------------------------------
    def test_e13_early_resume_rejected_before_cooldown_and_reconciliation(self) -> None:
        claim, contract = self._claim_and_build_contract()
        finish_worker_contract_job(
            self.root,
            contract,
            success=False,
            provider_error_code="RESOURCE_EXHAUSTED",
            retry_after_seconds=90,
            usage_source=None,
            now_utc=self.t0 + timedelta(seconds=2),
        )
        # 1. Early resume during cooldown -> rejected
        with self.assertRaisesRegex(BatchBudgetError, "GLOBAL_RATE_COOLDOWN_ACTIVE"):
            resume_global_rate_limit(
                self.root,
                self.plan_sha,
                actor="operator",
                reason="Attempted premature resume before cooldown window elapsed.",
                now_utc=self.t0 + timedelta(seconds=30),
            )
        # 2. Resume after cooldown but BEFORE reconciling unknown usage -> rejected
        with self.assertRaisesRegex(
            BatchBudgetError, "GLOBAL_RATE_UNSETTLED_USAGE_REQUIRES_RECONCILIATION"
        ):
            resume_global_rate_limit(
                self.root,
                self.plan_sha,
                actor="operator",
                reason="Cooldown elapsed but unsettled usage reservation still exists.",
                now_utc=self.t0 + timedelta(seconds=100),
            )
        # 3. Reconcile usage manually, then resume after cooldown -> succeeds
        reconcile_paused_reservation(
            self.root,
            self.plan_sha,
            claim["video_id"],
            actor="operator",
            reason="Reconciled unknown 429 usage after inspecting local diagnostics.",
            usage={"uncached_input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0},
            now_utc=self.t0 + timedelta(seconds=101),
        )
        resumed = resume_global_rate_limit(
            self.root,
            self.plan_sha,
            actor="operator",
            reason="Cooldown elapsed and all unsettled reservations reconciled.",
            now_utc=self.t0 + timedelta(seconds=102),
        )
        self.assertFalse(resumed["rate_limit"]["global_paused"])

    # -----------------------------------------------------------------------
    # E14: After-commit replay idempotency with verified files
    # -----------------------------------------------------------------------
    def test_e14_after_commit_replay_is_idempotent_and_rejects_mutation(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        vdir = self.root / "data" / "videos" / self.video_id
        in_m = build_stage_artifact_manifest(
            self.root,
            self.video_id,
            [vdir / "media" / "video.mp4", vdir / "media" / "captions.json3"],
        )
        out_m = build_stage_artifact_manifest(
            self.root, self.video_id, [vdir / "media_cache.json"]
        )

        with self.assertRaises(InjectedSyntheticCrash):
            record_verified_stage_artifact(
                self.root,
                contract,
                stage_name="media_cached",
                input_manifest=in_m,
                output_manifest=out_m,
                now_utc=self.t0,
                inject_fault="after_commit",
            )
        self.assertEqual(
            verified_stage_status(self.root, self.plan_sha, self.video_id)["stages_done"],
            1,
        )

        # Replay with identical on-disk files succeeds idempotently.
        replayed = record_verified_stage_artifact(
            self.root,
            contract,
            stage_name="media_cached",
            input_manifest=in_m,
            output_manifest=out_m,
            now_utc=self.t0 + timedelta(seconds=5),
        )
        self.assertTrue(replayed["replayed_existing_checkpoint"])
        self.assertEqual(replayed["stages_done"], 1)

        # Replay with a different output manifest fails closed.
        alt_file = vdir / "alt_cache.json"
        alt_file.write_text('{"alt": 1}', encoding="utf-8")
        alt_out = build_stage_artifact_manifest(self.root, self.video_id, [alt_file])
        with self.assertRaises(WorkerContractError) as ctx:
            record_verified_stage_artifact(
                self.root,
                contract,
                stage_name="media_cached",
                input_manifest=in_m,
                output_manifest=alt_out,
                now_utc=self.t0 + timedelta(seconds=6),
            )
        self.assertEqual(ctx.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx.exception.code, "STAGE_REPLAY_HASH_MISMATCH")

    # -----------------------------------------------------------------------
    # E15: Unauthorized external call and live mode blocked
    # -----------------------------------------------------------------------
    def test_e15_unauthorized_external_call_and_live_mode_blocked(self) -> None:
        _claim, contract = self._claim_and_build_contract()

        # 1. Live mode or allow_live_agy=True in contract is blocked immediately.
        for bad_patch in (
            {"execution_mode": "LIVE"},
            {"allow_live_agy": True},
            {"allow_external_network": True},
        ):
            with self.subTest(bad_patch=bad_patch):
                with self.assertRaises(WorkerContractError) as ctx:
                    validate_worker_input_contract(
                        self.root, {**contract, **bad_patch}, now_utc=self.t0
                    )
                self.assertEqual(ctx.exception.category, "AUTHORIZATION_FAILURE")

        # 2. Even after valid contract check, socket/subprocess/Agy calls are blocked.
        validate_worker_input_contract(self.root, contract, now_utc=self.t0)
        with self.assertRaises(WorkerContractError) as ctx_sock:
            socket.create_connection(("example.com", 443), timeout=1)
        self.assertEqual(ctx_sock.exception.category, "AUTHORIZATION_FAILURE")
        self.assertEqual(ctx_sock.exception.code, "UNAUTHORIZED_EXTERNAL_SOCKET_CALL")

        with self.assertRaises(WorkerContractError) as ctx_sub:
            subprocess.run(["cmd", "/c", "echo", "blocked"], check=False)
        self.assertEqual(ctx_sub.exception.category, "AUTHORIZATION_FAILURE")
        self.assertEqual(ctx_sub.exception.code, "UNAUTHORIZED_EXTERNAL_SUBPROCESS_CALL")

        with self.assertRaises(WorkerContractError) as ctx_agy:
            Agy._invoke(object(), ["agy"], "extractor", 1, 1)  # type: ignore[arg-type]
        self.assertEqual(ctx_agy.exception.category, "AUTHORIZATION_FAILURE")
        self.assertEqual(ctx_agy.exception.code, "UNAUTHORIZED_LIVE_AGY_CALL")

        self.assertGreaterEqual(self.guard.blocked_unauthorized_attempts, 3)

    # -----------------------------------------------------------------------
    # E16: Partial completion falsely marked complete & synthetic table isolation
    # -----------------------------------------------------------------------
    def test_e16_partial_completion_falsely_marked_complete_rejected(self) -> None:
        claim, contract = self._claim_and_build_contract()
        # Record only 3 of the 9 stages, then forge multimodal_checkpoint.json as COMPLETED.
        materialize_and_record_nine_stages(
            self.root, contract, now_utc=self.t0, up_to_stage=3
        )
        run_dir = (
            self.root / "data" / "videos" / self.video_id / "runs" / self.run_id
        )
        ckpt_file = run_dir / "multimodal_checkpoint.json"
        forged_ckpt = json.loads(ckpt_file.read_text(encoding="utf-8"))
        forged_ckpt["status"] = "COMPLETED"
        ckpt_file.write_text(json.dumps(forged_ckpt), encoding="utf-8")

        with self.assertRaises(WorkerContractError) as ctx1:
            finish_worker_contract_job(
                self.root,
                contract,
                success=True,
                usage_source=build_synthetic_stream_json_usage(),
                now_utc=self.t0 + timedelta(seconds=10),
            )
        self.assertEqual(ctx1.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx1.exception.code, "PARTIAL_STAGE_COMPLETION_REJECTED")

        # Also verify direct finish_batch_job cannot bypass the partial verified stage journal.
        with self.assertRaises(WorkerContractError) as ctx2:
            finish_batch_job(
                self.root,
                self.plan_sha,
                video_id=self.video_id,
                lease_token=claim["lease_token"],
                success=True,
                usage={"uncached_input_tokens": 100, "cache_read_tokens": 0, "output_tokens": 10},
                now_utc=self.t0 + timedelta(seconds=10),
            )
        self.assertEqual(ctx2.exception.category, "ARTIFACT_INTEGRITY_FAILURE")

    def test_synthetic_phase5d_checkpoints_never_satisfy_verified_contract(self) -> None:
        claim, contract = self._claim_and_build_contract()
        # Record all 9 Phase 5D synthetic-only stages (strings only, no verified files).
        for idx, stage_name in enumerate(MULTIMODAL_STAGES):
            record_synthetic_stage(
                self.root,
                self.plan_sha,
                self.video_id,
                lease_token=claim["lease_token"],
                stage_name=stage_name,
                input_sha256=f"{idx + 1:064x}",
                output_sha256=f"{idx + 100:064x}",
                now_utc=self.t0,
            )
        # Phase 5E verified status remains at 0 stages and rejects completion.
        self.assertEqual(
            verified_stage_status(self.root, self.plan_sha, self.video_id)["stages_done"],
            0,
        )
        with self.assertRaises(WorkerContractError) as ctx:
            finish_worker_contract_job(
                self.root,
                contract,
                success=True,
                usage_source=build_synthetic_stream_json_usage(),
                now_utc=self.t0 + timedelta(seconds=10),
            )
        self.assertEqual(ctx.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx.exception.code, "PARTIAL_STAGE_COMPLETION_REJECTED")

    # -----------------------------------------------------------------------
    # Additional Contract & Safety Validation Tests
    # -----------------------------------------------------------------------
    def test_multi_call_slot_authorization_contract_flash_and_argon(self) -> None:
        slots = [
            {
                "call_index": 1,
                "call_id": "call-primary-flash-01",
                "call_role": "primary_analysis",
                "exact_model_id": DEFAULT_FLASH_MODEL,
                "reserved_tokens": {
                    "uncached_input_tokens": 8000,
                    "cache_read_tokens": 1000,
                    "output_tokens": 1500,
                },
            },
            {
                "call_index": 2,
                "call_id": "call-verifier-argon-02",
                "call_role": "argon_selective_verifier",
                "exact_model_id": DEFAULT_ARGON_MODEL,
                "reserved_tokens": {
                    "uncached_input_tokens": 4000,
                    "cache_read_tokens": 500,
                    "output_tokens": 800,
                },
            },
        ]
        _claim, contract = self._claim_and_build_contract(call_slots=slots)
        validated = validate_worker_input_contract(self.root, contract, now_utc=self.t0)
        slot_auth = validated["call_slot_authorization"]
        self.assertEqual(slot_auth["slot_count"], 2)
        self.assertFalse(slot_auth["multi_model_live_execution_enabled"])
        self.assertEqual(slot_auth["total_reserved_tokens"], 15800)

        # Duplicate call_id is rejected
        dup_slots = [slots[0], dict(slots[1], call_id="call-primary-flash-01")]
        with self.assertRaises(WorkerContractError) as ctx:
            validate_worker_input_contract(
                self.root, {**contract, "call_slots": dup_slots}, now_utc=self.t0
            )
        self.assertEqual(ctx.exception.category, "CONTRACT_VIOLATION")

    def test_stage_manifest_linkage_broken_rejected(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        vdir = self.root / "data" / "videos" / self.video_id
        in0 = build_stage_artifact_manifest(
            self.root,
            self.video_id,
            [vdir / "media" / "video.mp4", vdir / "media" / "captions.json3"],
        )
        out0 = build_stage_artifact_manifest(
            self.root, self.video_id, [vdir / "media_cache.json"]
        )
        record_verified_stage_artifact(
            self.root,
            contract,
            stage_name="media_cached",
            input_manifest=in0,
            output_manifest=out0,
            now_utc=self.t0,
        )

        # Stage 1 (subtitles_parsed) omits media_cache.json from its input_manifest!
        parsed = vdir / "parsed_subtitles.json"
        parsed.write_text('{"segments":[]}', encoding="utf-8")
        unlinked_in1 = build_stage_artifact_manifest(
            self.root, self.video_id, [vdir / "media" / "captions.json3"]
        )
        out1 = build_stage_artifact_manifest(self.root, self.video_id, [parsed])
        with self.assertRaises(WorkerContractError) as ctx:
            record_verified_stage_artifact(
                self.root,
                contract,
                stage_name="subtitles_parsed",
                input_manifest=unlinked_in1,
                output_manifest=out1,
                now_utc=self.t0 + timedelta(seconds=1),
            )
        self.assertEqual(ctx.exception.category, "ARTIFACT_INTEGRITY_FAILURE")
        self.assertEqual(ctx.exception.code, "STAGE_INPUT_OUTPUT_LINKAGE_BROKEN")

    def test_error_classification_and_redaction_cover_all_eight_categories(self) -> None:
        self.assertEqual(len(WORKER_ERROR_CATEGORIES), 8)
        for cat in WORKER_ERROR_CATEGORIES:
            err = WorkerContractError(cat, "SAMPLE_TEST_CODE")
            info = classify_worker_error(err)
            self.assertEqual(info["error_category"], cat)
            self.assertFalse(info["automatic_retry_allowed"])
            self.assertFalse(info["raw_provider_logged"])

        fake_google_key = "AI" + "za" + "0" * 32
        raw_secret = (
            "Provider failed: Bearer eyJhbGciOiJIUzI1NiJ9 "
            f"api_key={fake_google_key} "
            "lease_token=0123456789abcdef0123456789abcdef0123456789abcdef "
            "https://accounts.google.com/o/oauth2/auth?token=secret"
        )
        redacted = sanitize_audit_text(raw_secret)
        self.assertNotIn(fake_google_key, redacted)
        self.assertNotIn("0123456789abcdef0123456789abcdef0123456789abcdef", redacted)
        self.assertNotIn("https://accounts.google.com", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_input_contract_field_tampering_rejected(self) -> None:
        _claim, contract = self._claim_and_build_contract()
        for bad_patch, expected_code in (
            ({"source_ids": ["S999"]}, "SOURCE_IDS_LEDGER_MISMATCH"),
            ({"run_id": "../bad_run_id"}, "RUN_ID_FORMAT_INVALID"),
            ({"attempt": 2}, "ATTEMPT_COUNT_MISMATCH"),
            ({"exact_model_id": "auto"}, "EXACT_MODEL_ID_REQUIRED"),
            (
                {"budget_policy": {"budget_id": "wrong", "policy_sha256": "0" * 64}},
                "CONTRACT_BUDGET_POLICY_MISMATCH",
            ),
        ):
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(WorkerContractError) as ctx:
                    validate_worker_input_contract(
                        self.root, {**contract, **bad_patch}, now_utc=self.t0
                    )
                self.assertEqual(ctx.exception.category, "CONTRACT_VIOLATION")
                self.assertEqual(ctx.exception.code, expected_code)

    def test_phase5e_suite_runner_executes_all_sixteen_scenarios(self) -> None:
        from ytresearch.batch_worker_contract import run_phase5e_contract_validation_suite

        report = run_phase5e_contract_validation_suite()
        self.assertEqual(report["total_scenarios"], 16)
        self.assertEqual(report["passed_scenarios"], 16)
        self.assertEqual(report["failed_scenarios"], 0)
        self.assertEqual(
            [s["scenario_id"] for s in report["scenarios"]],
            [f"E{i:02d}" for i in range(1, 17)],
        )
        self.assertEqual(report["external_calls"]["socket_calls"], 0)
        self.assertEqual(report["external_calls"]["subprocess_calls"], 0)
        self.assertEqual(report["external_calls"]["live_agy_calls"], 0)
        self.assertEqual(report["external_calls"]["youtube_calls"], 0)
        self.assertGreaterEqual(
            report["external_calls"]["blocked_unauthorized_attempts"], 1
        )


if __name__ == "__main__":
    unittest.main()
