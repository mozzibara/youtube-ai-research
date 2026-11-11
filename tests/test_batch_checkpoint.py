"""Phase 5D checkpoint crash/replay tests: synthetic only, no AGY usage."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ytresearch.batch_checkpoint import (
    InjectedSyntheticCrash, SyntheticStageInvalid,
    record_synthetic_stage, synthetic_stage_status,
)
from ytresearch.batch_plan import plan_research_batch
from ytresearch.batch_queue import (
    initialize_batch_queue, claim_batch_job, finish_batch_job,
    recover_batch_job, batch_queue_status,
)
from ytresearch.multimodal import MULTIMODAL_STAGES


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CrashSafeStageTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        (self.root / "research").mkdir()
        (self.root / "research" / "index.json").write_text(json.dumps({
            "source_count": 77, "sources": [
                {"source_id": f"S{i:03d}", "source_ids": [f"S{i:03d}"],
                 "video_id": f"{i:011d}", "playlist_position": i}
                for i in range(1, 78)
            ],
        }), encoding="utf-8")
        self.video = f"{1:011d}"
        folder = self.root / "data" / "videos" / self.video
        folder.mkdir(parents=True)
        media = folder / "video.mp4"
        sub = folder / "subtitle.json3"
        media.write_bytes(b"synthetic only")
        sub.write_text("{}", encoding="utf-8")
        (folder / "media_cache.json").write_text(json.dumps({
            "assets": {"video": {"cached_path": str(media)},
                       "subtitle_json3": {"cached_path": str(sub)}}
        }), encoding="utf-8")
        self.plan = plan_research_batch(self.root, max_jobs=1, save=True)
        self.plan_sha = self.plan["plan_sha256"]
        initialize_batch_queue(self.root, self.plan_sha)
        self.now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        self.claim = claim_batch_job(
            self.root, self.plan_sha, owner="synthetic-worker", now_utc=self.now,
        )

    def stage(self, index, *, token=None, now=None, input_hash=None,
              output_hash=None, fault=None):
        return record_synthetic_stage(
            self.root, self.plan_sha, self.video,
            lease_token=token or self.claim["lease_token"],
            stage_name=MULTIMODAL_STAGES[index],
            input_sha256=input_hash or _sha(f"input:{index}"),
            output_sha256=output_hash or _sha(f"output:{index}"),
            now_utc=now or self.now,
            inject_fault=fault,
        )

    def status(self):
        return synthetic_stage_status(self.root, self.plan_sha, self.video)

    def test_enforces_nine_stage_order_without_real_outputs(self):
        initial = self.status()
        self.assertEqual(initial["stages_done"], 0)
        self.assertEqual(initial["next_stage"], MULTIMODAL_STAGES[0])
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_OUT_OF_ORDER"):
            self.stage(1)
        for i in range(len(MULTIMODAL_STAGES)):
            stage = self.stage(i)
            self.assertEqual(stage["stages_done"], i + 1)
        final = self.status()
        self.assertTrue(final["synthetic_stage_complete"])
        self.assertIsNone(final["next_stage"])
        self.assertFalse(final["production_analysis_completed"])
        self.assertFalse(final["real_artifact_contents_verified"])
        result = finish_batch_job(
            self.root, self.plan_sha, video_id=self.video,
            lease_token=self.claim["lease_token"], success=True,
            now_utc=self.now + timedelta(seconds=5),
        )
        self.assertEqual(result["status"], "SIMULATED")
        self.assertFalse((self.root / "data" / "videos" / self.video / "runs").exists())

    def test_partial_stage_sequence_prevents_successful_finish(self):
        self.stage(0)
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_SIMULATED_COMPLETION_INCOMPLETE"):
            finish_batch_job(
                self.root, self.plan_sha, video_id=self.video,
                lease_token=self.claim["lease_token"], success=True,
                now_utc=self.now + timedelta(seconds=2),
            )
        self.assertEqual(batch_queue_status(self.root, self.plan_sha)["counts"], {"LEASED": 1})

    def test_before_and_after_insert_crashes_roll_back(self):
        for fault in ("before_insert", "after_insert"):
            with self.subTest(fault=fault), self.assertRaises(InjectedSyntheticCrash):
                self.stage(0, fault=fault)
            self.assertEqual(self.status()["stages_done"], 0)
        result = self.stage(0)
        self.assertEqual(result["stages_done"], 1)

    def test_after_commit_crash_can_replay_exactly_once(self):
        with self.assertRaisesRegex(InjectedSyntheticCrash, "INJECTED_AFTER_STAGE_COMMIT"):
            self.stage(0, fault="after_commit")
        self.assertEqual(self.status()["stages_done"], 1)
        again = self.stage(0)
        self.assertTrue(again["replayed_existing_checkpoint"])
        self.assertEqual(self.status()["stages_done"], 1)
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_REPLAY_HASH_MISMATCH"):
            self.stage(0, input_hash=_sha("different source input"))
        self.assertEqual(self.status()["stages_done"], 1)

    def test_expired_lease_manual_recovery_preserves_checkpoint_and_replays(self):
        self.stage(0)
        past = self.now + timedelta(minutes=31)
        recover_batch_job(
            self.root, self.plan_sha, video_id=self.video,
            actor="operator",
            reason="Interrupted synthetic lease is expired and must be released.",
            now_utc=past,
        )
        recover_batch_job(
            self.root, self.plan_sha, video_id=self.video,
            actor="operator",
            reason="Human-approved retry after checking the prior synthetic output.",
            retry=True, now_utc=past,
        )
        new_lease = claim_batch_job(
            self.root, self.plan_sha, owner="synthetic-second-attempt",
            now_utc=past,
        )
        self.assertNotEqual(self.claim["lease_token"], new_lease["lease_token"])
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_ACTIVE_LEASE_REQUIRED"):
            self.stage(0, token=self.claim["lease_token"], now=past)
        self.claim = new_lease
        replay = self.stage(0, now=past)
        self.assertTrue(replay["replayed_existing_checkpoint"])
        self.assertEqual(self.status()["stages"][0]["first_attempt"], 1)
        self.assertEqual(self.stage(1, now=past)["stages_done"], 2)

    def test_checkpoint_event_hash_tamper_fails_closed(self):
        self.stage(0)
        db_file = self.root / "data" / "batch_dryrun_queue.sqlite3"
        with closing(sqlite3.connect(db_file)) as db:
            db.execute(
                "UPDATE batch_synthetic_stages SET output_sha256=? "
                "WHERE plan_sha=? AND video_id=? AND stage_index=0",
                (_sha("forged output"), self.plan_sha, self.video),
            )
            db.commit()
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_CHECKPOINT_CHAIN_CORRUPTED"):
            self.status()
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_CHECKPOINT_CHAIN_CORRUPTED"):
            self.stage(1)

    def test_second_stage_must_have_correct_active_lease_and_valid_sha(self):
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_INPUT_OR_OUTPUT_HASH_INVALID"):
            self.stage(0, input_hash="wrong")
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_ACTIVE_LEASE_REQUIRED"):
            self.stage(0, token="not-a-lease")
        with self.assertRaisesRegex(SyntheticStageInvalid, "STAGE_LEASE_EXPIRED"):
            self.stage(0, now=self.now + timedelta(minutes=31))
        self.assertEqual(self.status()["stages_done"], 0)

    def test_stages_never_modify_existing_video_file(self):
        original = (self.root / "data" / "videos" / self.video / "video.mp4").read_bytes()
        for i in range(3):
            self.stage(i)
        self.assertEqual((self.root / "data" / "videos" / self.video / "video.mp4").read_bytes(),
                         original)
        self.assertNotIn("lease_token", json.dumps(self.status()))


if __name__ == "__main__":
    unittest.main()
