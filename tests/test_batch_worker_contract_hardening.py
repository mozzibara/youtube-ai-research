"""Phase 5E.1 regression tests for owner binding and write-time lease fencing.

Each racing writer uses its own SQLite connection in a separate thread and
runs only after full prevalidation but before the worker starts BEGIN IMMEDIATE.
No network, subprocess, AGY, or actual media processing is performed.
"""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
import sqlite3
import tempfile
import unittest

import ytresearch.batch_worker_contract as worker
from ytresearch.batch_queue import claim_batch_job


class WorkerContractFencingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase5e1_fencing_")
        self.addCleanup(tmp.cleanup)
        guard = worker.offline_external_call_guard()
        self.guard = guard.__enter__()
        self.addCleanup(lambda: guard.__exit__(None, None, None))
        ws = worker.create_phase5e_isolated_workspace(Path(tmp.name))
        self.root = ws["root"]
        self.plan_sha = ws["plan_sha256"]
        self.t0 = ws["t0"]
        self.video_id = f"{1:011d}"
        self.db_path = self.root / "data" / "batch_dryrun_queue.sqlite3"
        claim = claim_batch_job(
            self.root, self.plan_sha, owner="phase5e1-worker",
            model=worker.DEFAULT_FLASH_MODEL, now_utc=self.t0,
        )
        self.contract = worker.build_worker_input_contract(
            self.root, plan_sha256=self.plan_sha, video_id=self.video_id,
            run_id="run_phase5e1", exact_model_id=worker.DEFAULT_FLASH_MODEL,
            lease_token=claim["lease_token"],
            lease_expires_utc=claim["lease_expires_utc"], attempt=1,
        )

    def _stage_zero(self, contract=None) -> None:
        c = contract or self.contract
        media = self.root / "data" / "videos" / self.video_id / "media"
        cached = self.root / "data" / "videos" / self.video_id / "media_cache.json"
        input_manifest = worker.build_stage_artifact_manifest(
            self.root, self.video_id, [media / "video.mp4", media / "captions.json3"]
        )
        output_manifest = worker.build_stage_artifact_manifest(
            self.root, self.video_id, [cached]
        )
        worker.record_verified_stage_artifact(
            self.root, c, stage_name="media_cached",
            input_manifest=input_manifest, output_manifest=output_manifest,
            now_utc=self.t0 + timedelta(seconds=1),
        )

    def _race_after_validation(self, statement: str, params: tuple, action) -> None:
        original = worker.validate_worker_input_contract
        fired = False

        def validate_then_race(*args, **kwargs):
            nonlocal fired
            verified = original(*args, **kwargs)
            if not fired:
                fired = True
                done = Event()
                errors: list[Exception] = []

                def writer():
                    try:
                        with closing(sqlite3.connect(self.db_path, timeout=5)) as db:
                            changed = db.execute(statement, params)
                            if changed.rowcount != 1:
                                raise AssertionError(
                                    f"Interleaved SQLite update changed {changed.rowcount} rows"
                                )
                            db.commit()
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        done.set()

                thread = Thread(target=writer, name="interleaved-queue-writer")
                thread.start()
                if not done.wait(5):
                    raise AssertionError("Interleaved writer did not finish")
                thread.join(timeout=2)
                if errors:
                    raise errors[0]
            return verified

        with patch.object(
            worker, "validate_worker_input_contract", side_effect=validate_then_race
        ):
            action()
        self.assertTrue(fired, "Test never reached the intended validation/write gap")

    def _assert_reserved(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            status = db.execute(
                "SELECT status FROM budget_reservations "
                "WHERE plan_sha=? AND video_id=? AND attempt=1",
                (self.plan_sha, self.video_id),
            ).fetchone()
            self.assertIsNotNone(status)
            self.assertEqual(status[0], "RESERVED")
            settled = db.execute(
                "SELECT count(*) FROM worker_usage_settlements"
            ).fetchone()[0]
            self.assertEqual(settled, 0)

    def test_builder_binds_actual_queue_owner(self) -> None:
        self.assertEqual(self.contract["owner"], "phase5e1-worker")
        verified = worker.validate_worker_input_contract(
            self.root, self.contract, now_utc=self.t0
        )
        self.assertTrue(verified["contract_valid"])

    def test_forged_owner_rejected_even_with_valid_lease_token(self) -> None:
        forged = dict(self.contract, owner="someone-else")
        with self.assertRaises(worker.WorkerContractError) as ctx:
            worker.validate_worker_input_contract(self.root, forged, now_utc=self.t0)
        self.assertEqual(ctx.exception.category, "AUTHORIZATION_FAILURE")
        self.assertEqual(ctx.exception.code, "LEASE_OWNER_MISMATCH")
        self.assertNotIn(self.contract["lease_token"], str(ctx.exception))

    def test_stage_rejects_interleaved_owner_change(self) -> None:
        with self.assertRaises(worker.WorkerContractError) as ctx:
            self._race_after_validation(
                "UPDATE queue_jobs SET owner=? WHERE plan_sha=? AND video_id=?",
                ("other-worker", self.plan_sha, self.video_id),
                self._stage_zero,
            )
        self.assertEqual(ctx.exception.code, "LEASE_OWNER_MISMATCH")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM batch_verified_stage_artifacts").fetchone()[0],
                0,
            )
        self._assert_reserved()

    def test_stage_rejects_interleaved_expiry(self) -> None:
        expired = (self.t0 - timedelta(seconds=1)).isoformat()
        with self.assertRaises(worker.WorkerContractError) as ctx:
            self._race_after_validation(
                "UPDATE queue_jobs SET lease_expires_utc=? WHERE plan_sha=? AND video_id=?",
                (expired, self.plan_sha, self.video_id),
                self._stage_zero,
            )
        self.assertEqual(ctx.exception.category, "LEASE_EXPIRED")
        self._assert_reserved()

    def test_stage_rejects_interleaved_global_pause(self) -> None:
        with self.assertRaises(worker.WorkerContractError) as ctx:
            self._race_after_validation(
                "INSERT INTO global_rate_state (singleton,paused,updated_utc) "
                "VALUES (1,1,?) ON CONFLICT(singleton) DO UPDATE SET paused=1",
                (self.t0.isoformat(),),
                self._stage_zero,
            )
        self.assertEqual(ctx.exception.category, "GLOBAL_RATE_LIMIT")
        self._assert_reserved()

    def test_finish_failure_rejects_interleaved_lease_token_replacement(self) -> None:
        with self.assertRaises(worker.WorkerContractError) as ctx:
            self._race_after_validation(
                "UPDATE queue_jobs SET lease_token=? WHERE plan_sha=? AND video_id=?",
                ("f" * 48, self.plan_sha, self.video_id),
                lambda: worker.finish_worker_contract_job(
                    self.root, self.contract, success=False,
                    provider_error_code="503_SERVICE_UNAVAILABLE",
                    now_utc=self.t0 + timedelta(seconds=2),
                ),
            )
        self.assertEqual(ctx.exception.code, "LEASE_TOKEN_INVALID")
        with closing(sqlite3.connect(self.db_path)) as db:
            status = db.execute(
                "SELECT status FROM queue_jobs WHERE plan_sha=? AND video_id=?",
                (self.plan_sha, self.video_id),
            ).fetchone()[0]
            self.assertEqual(status, "LEASED")
        self._assert_reserved()

    def test_finish_success_rejects_interleaved_recovery(self) -> None:
        worker.materialize_and_record_nine_stages(
            self.root, self.contract, now_utc=self.t0
        )
        with self.assertRaises(worker.WorkerContractError) as ctx:
            self._race_after_validation(
                "UPDATE queue_jobs SET status='PAUSED_EXPIRED',owner=NULL,"
                "lease_token=NULL,lease_expires_utc=NULL "
                "WHERE plan_sha=? AND video_id=?",
                (self.plan_sha, self.video_id),
                lambda: worker.finish_worker_contract_job(
                    self.root, self.contract, success=True,
                    usage_source=worker.build_synthetic_stream_json_usage(),
                    now_utc=self.t0 + timedelta(seconds=20),
                ),
            )
        self.assertEqual(ctx.exception.code, "QUEUE_JOB_NOT_LEASED")
        with closing(sqlite3.connect(self.db_path)) as db:
            status = db.execute(
                "SELECT status FROM queue_jobs WHERE plan_sha=? AND video_id=?",
                (self.plan_sha, self.video_id),
            ).fetchone()[0]
            self.assertEqual(status, "PAUSED_EXPIRED")
        self._assert_reserved()

    def test_finish_rejects_interleaved_attempt_change(self) -> None:
        with self.assertRaises(worker.WorkerContractError) as ctx:
            self._race_after_validation(
                "UPDATE queue_jobs SET attempts=2 WHERE plan_sha=? AND video_id=?",
                (self.plan_sha, self.video_id),
                lambda: worker.finish_worker_contract_job(
                    self.root, self.contract, success=False,
                    provider_error_code="503_SERVICE_UNAVAILABLE",
                    now_utc=self.t0 + timedelta(seconds=2),
                ),
            )
        self.assertEqual(ctx.exception.code, "ATTEMPT_COUNT_MISMATCH")
        self._assert_reserved()

    def test_run_id_cannot_change_mid_chain(self) -> None:
        self._stage_zero()
        changed_run = dict(self.contract, run_id="different_run")
        out = self.root / "data" / "videos" / self.video_id / "parsed_subtitles.json"
        out.write_text('{"offline":true}', encoding="utf-8")
        sub = self.root / "data" / "videos" / self.video_id / "media" / "captions.json3"
        cache = self.root / "data" / "videos" / self.video_id / "media_cache.json"
        in_manifest = worker.build_stage_artifact_manifest(
            self.root, self.video_id, [cache, sub]
        )
        out_manifest = worker.build_stage_artifact_manifest(
            self.root, self.video_id, [out]
        )
        with self.assertRaises(worker.WorkerContractError) as ctx:
            worker.record_verified_stage_artifact(
                self.root, changed_run, stage_name="subtitles_parsed",
                input_manifest=in_manifest, output_manifest=out_manifest,
                now_utc=self.t0 + timedelta(seconds=2),
            )
        self.assertEqual(ctx.exception.code, "RUN_ID_CHAIN_MISMATCH")
        status = worker.verified_stage_status(self.root, self.plan_sha, self.video_id)
        self.assertEqual(status["stages_done"], 1)
        self.assertEqual(status["run_id"], self.contract["run_id"])
        self._assert_reserved()


if __name__ == "__main__":
    unittest.main()
