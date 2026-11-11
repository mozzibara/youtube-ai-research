"""Phase 5B single-worker lease is simulation-only and fail-closed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ytresearch.batch_plan import plan_research_batch
from ytresearch.batch_queue import (
    BatchQueueError, initialize_batch_queue, batch_queue_status,
    claim_batch_job, finish_batch_job, recover_batch_job,
)


class BatchLeaseDryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/"research").mkdir()
        self.index = self.root/"research"/"index.json"
        self.index.write_text(json.dumps({
            "source_count":77, "sources":[
                {"source_id":f"S{i:03d}","source_ids":[f"S{i:03d}"],
                 "video_id":f"{i:011d}","playlist_position":i}
                for i in range(1,78)
            ]
        }), encoding="utf-8")
        for n in (1,2,3):
            vid=f"{n:011d}"
            folder=self.root/"data"/"videos"/vid
            folder.mkdir(parents=True)
            video=folder/"video.mp4"
            subtitle=folder/"captions.json3"
            video.write_bytes(b"synthetic")
            subtitle.write_text("{}", encoding="utf-8")
            (folder/"media_cache.json").write_text(json.dumps({
                "assets": {
                    "video":{"cached_path":str(video)},
                    "subtitle_json3":{"cached_path":str(subtitle)}
                }
            }),encoding="utf-8")
        self.original=self.index.read_bytes()
        self.plan=plan_research_batch(self.root,max_jobs=2,save=True)
        self.sha=self.plan["plan_sha256"]
        self.t0=datetime(2026,11,11,0,0,0,tzinfo=timezone.utc)

    def test_init_is_idempotent_and_no_actual_video_analysis(self):
        a=initialize_batch_queue(self.root,self.sha)
        b=initialize_batch_queue(self.root,self.sha)
        self.assertEqual(a,b)
        self.assertEqual(a["counts"],{"PENDING":2})
        self.assertEqual(a["mode"],"SYNTHETIC_DRYRUN_ONLY")
        self.assertFalse(a["live_agy_enabled"])
        self.assertEqual(self.index.read_bytes(),self.original)
        self.assertFalse((self.root/"data"/"research_state.sqlite3").exists())
        self.assertFalse((self.root/"data"/"videos"/"00000000001"/"runs").exists())

    def test_serial_single_worker_prevents_duplicate_claim(self):
        initialize_batch_queue(self.root,self.sha)
        claim=claim_batch_job(self.root,self.sha,owner="worker1",now_utc=self.t0)
        self.assertEqual(claim["video_id"],f"{1:011d}")
        self.assertFalse(claim["network_or_model_execution"])
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_ACTIVE_OR_EXPIRED_LEASE"):
            claim_batch_job(self.root,self.sha,owner="worker2",now_utc=self.t0)
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_LEASE_TOKEN_OR_STATUS_INVALID"):
            finish_batch_job(self.root,self.sha,video_id=claim["video_id"],
                             lease_token="wrong",success=True,now_utc=self.t0)
        result=finish_batch_job(self.root,self.sha,video_id=claim["video_id"],
                                 lease_token=claim["lease_token"],success=True,
                                 now_utc=self.t0+timedelta(seconds=10))
        self.assertEqual(result["status"],"SIMULATED")
        b=claim_batch_job(self.root,self.sha,owner="worker2",
                          now_utc=self.t0+timedelta(seconds=20))
        self.assertEqual(b["video_id"],f"{2:011d}")
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],
                         {"LEASED":1,"SIMULATED":1})
        self.assertNotIn("lease_token",str(batch_queue_status(self.root,self.sha)))

    def test_expired_lease_pauses_until_explicit_recovery(self):
        initialize_batch_queue(self.root,self.sha)
        claim=claim_batch_job(self.root,self.sha,owner="worker",
                              now_utc=self.t0)
        expired=self.t0+timedelta(minutes=31)
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_LEASE_EXPIRED"):
            finish_batch_job(self.root,self.sha,video_id=claim["video_id"],
                             lease_token=claim["lease_token"],success=True,
                             now_utc=expired)
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_ACTIVE_OR_EXPIRED_LEASE"):
            claim_batch_job(self.root,self.sha,owner="other",now_utc=expired)
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_LEASE_NOT_YET_EXPIRED"):
            recover_batch_job(self.root,self.sha,video_id=claim["video_id"],
                              actor="reviewer",reason="A manual worker check requires explicit recovery.",
                              now_utc=self.t0+timedelta(minutes=1))
        paused=recover_batch_job(self.root,self.sha,video_id=claim["video_id"],
                             actor="reviewer",reason="The lease expired and recovery is manually authorized.",
                             now_utc=expired)
        self.assertEqual(paused["status"],"PAUSED_EXPIRED")
        reset=recover_batch_job(self.root,self.sha,video_id=claim["video_id"],
                              actor="reviewer",reason="The prior synthetic attempt is safe to retry.",
                              retry=True,now_utc=expired)
        self.assertEqual(reset["status"],"PENDING")

    def test_failed_requires_explicit_retry_and_attempt_cap(self):
        initialize_batch_queue(self.root,self.sha)
        reason="Explicit manual pause and authorized retry of synthetic job."
        first=claim_batch_job(self.root,self.sha,owner="worker",now_utc=self.t0)
        finish_batch_job(self.root,self.sha,video_id=first["video_id"],
                         lease_token=first["lease_token"],success=False,
                         now_utc=self.t0+timedelta(seconds=2))
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_RETRY_REQUIRES_PAUSED_JOB"):
            recover_batch_job(self.root,self.sha,video_id=f"{2:011d}",
                              actor="operator",reason=reason,retry=True,now_utc=self.t0)
        recover_batch_job(self.root,self.sha,video_id=first["video_id"],
                          actor="operator",reason=reason,retry=True,now_utc=self.t0)
        second=claim_batch_job(self.root,self.sha,owner="worker",now_utc=self.t0)
        finish_batch_job(self.root,self.sha,video_id=second["video_id"],
                         lease_token=second["lease_token"],success=False,
                         now_utc=self.t0+timedelta(seconds=2))
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_MAX_ATTEMPTS_EXCEEDED"):
            recover_batch_job(self.root,self.sha,video_id=second["video_id"],
                              actor="operator",reason=reason,retry=True,now_utc=self.t0)

    def test_stale_plan_cannot_be_claimed(self):
        initialize_batch_queue(self.root,self.sha)
        cache=self.root/"data"/"videos"/f"{1:011d}"/"media_cache.json"
        cache.unlink()
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_PLAN_STALE"):
            claim_batch_job(self.root,self.sha,owner="operator",now_utc=self.t0)
        self.assertEqual(batch_queue_status(self.root,self.sha)["counts"],
                         {"PENDING":2})

    def test_tampered_plan_cannot_initialize(self):
        filepath=Path(self.plan["saved_plan_path"])
        payload=json.loads(filepath.read_text(encoding="utf-8"))
        payload["production_live_batch_authorized"]=True
        filepath.write_text(json.dumps(payload),encoding="utf-8")
        with self.assertRaisesRegex(BatchQueueError,"QUEUE_PLAN_CONTENT_TAMPERED"):
            initialize_batch_queue(self.root,self.sha)

    def test_queue_rejects_unknown_plan_or_wrong_format(self):
        for sha in ("bad","0"*64):
            with self.subTest(sha=sha),self.assertRaises(BatchQueueError):
                initialize_batch_queue(self.root,sha)

    def test_source_index_never_modified(self):
        initialize_batch_queue(self.root,self.sha)
        claim=claim_batch_job(self.root,self.sha,owner="test",now_utc=self.t0)
        finish_batch_job(self.root,self.sha,video_id=claim["video_id"],
                         lease_token=claim["lease_token"],success=True,
                         now_utc=self.t0+timedelta(minutes=1))
        self.assertEqual(self.index.read_bytes(),self.original)


if __name__ == "__main__":
    unittest.main()
