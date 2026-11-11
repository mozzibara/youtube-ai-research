"""Phase 5 synthetic-only batch planning: no network, downloads or AGY usage."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from ytresearch.batch_plan import BatchPlanBlocked, plan_research_batch
from ytresearch.research_state import open_research_db, sync_research_sources


class OfflineBatchPlanTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name)
        (self.root / "research").mkdir()
        self.index_file = self.root / "research" / "index.json"
        self.index_file.write_text(json.dumps({
            "source_count": 77, "source_entries_count": 77,
            "sources": [
                {"source_id": f"S{i:03d}", "source_ids": [f"S{i:03d}"],
                 "video_id": f"{i:011d}", "playlist_position": i,
                 "status": "not_started"} for i in range(1,78)
            ],
        }), encoding="utf-8")

    def _cache(self, number, *, video=True, subtitle=True):
        vid = f"{number:011d}"
        directory = self.root / "data" / "videos" / vid
        directory.mkdir(parents=True, exist_ok=True)
        media = directory / "video.mp4"
        sub = directory / "captions.json3"
        if video:
            media.write_bytes(b"synthetic mp4 placeholder")
        if subtitle:
            sub.write_text("{}", encoding="utf-8")
        (directory / "media_cache.json").write_text(json.dumps({
            "assets": {
                "video": {"cached_path": str(media)},
                "subtitle_json3": {"cached_path": str(sub)},
            }
        }), encoding="utf-8")

    def _state(self, video, run_status, *, approved=False):
        vid = f"{video:011d}"
        with closing(open_research_db(self.root)) as db:
            with db:
                db.execute("""
                    INSERT INTO analysis_runs
                    (video_id,run_id,status,checkpoint_path,updated_utc)
                    VALUES (?,?,?,?,?)
                """, (vid, "sample_run", run_status, "fixture_path",
                      "2026-11-11T08:00:00Z"))
                if approved:
                    db.execute("""
                        INSERT INTO reports
                        (video_id,run_id,report_kind,path,review_state,updated_utc)
                        VALUES (?,?,?,?,?,?)
                    """, (vid,"sample_run","RESEARCH_19","fixture_report.md",
                          "APPROVED","2026-11-11T08:00:00Z"))

    def test_dryrun_no_database_no_media_and_no_mutation(self):
        before = self.index_file.read_bytes()
        output = plan_research_batch(self.root)
        self.assertEqual(output["source_entries"], 77)
        self.assertEqual(output["unique_videos"], 77)
        self.assertEqual(output["disposition_counts"], {"HOLD_MEDIA_CACHE_MISSING": 77})
        self.assertEqual(output["plan_only_video_ids"], [])
        self.assertFalse(output["production_live_batch_authorized"])
        self.assertEqual(output["live_gate"], "PILOT_REPORT_APPROVAL_REQUIRED")
        self.assertEqual(before, self.index_file.read_bytes())
        self.assertFalse((self.root / "data" / "research_state.sqlite3").exists())
        self.assertFalse((self.root / "data" / "batch_plans").exists())

    def test_queue_order_skips_completed_failed_inconsistent_and_approved(self):
        sync_research_sources(self.root)
        for n in range(1,8):
            self._cache(n)
        self._state(1, "COMPLETED")
        self._state(2, "COMPLETED", approved=True)
        self._state(3, "FAILED")
        self._state(4, "INCONSISTENT")
        result = plan_research_batch(self.root, max_jobs=2)
        lookup = {row["video_id"]: row for row in result["jobs"]}
        self.assertEqual(lookup[f"{1:011d}"]["disposition"], "SKIP_COMPLETED_REVIEW_REQUIRED")
        self.assertEqual(lookup[f"{2:011d}"]["disposition"], "SKIP_APPROVED")
        self.assertEqual(lookup[f"{3:011d}"]["disposition"], "HOLD_FAILED_NEEDS_EXPLICIT_RETRY")
        self.assertEqual(lookup[f"{4:011d}"]["disposition"], "HOLD_INCONSISTENT")
        self.assertEqual(lookup[f"{5:011d}"]["disposition"], "PLAN_ONLY")
        self.assertEqual(lookup[f"{6:011d}"]["disposition"], "PLAN_ONLY")
        self.assertEqual(lookup[f"{7:011d}"]["disposition"], "DEFER_CAP")
        self.assertEqual(result["plan_only_video_ids"], [f"{5:011d}", f"{6:011d}"])
        self.assertEqual(len(result["jobs"]), 77)
        self.assertFalse(result["production_live_batch_authorized"])

    def test_failed_requires_retry_intent_and_never_executes(self):
        sync_research_sources(self.root)
        for n in (3,4,5):
            self._cache(n)
        self._state(3, "FAILED")
        self._state(4, "IN_PROGRESS")
        a = plan_research_batch(self.root, max_jobs=2)
        b = plan_research_batch(self.root, max_jobs=2, retry_failed=True)
        self.assertEqual(a["plan_only_video_ids"], [f"{5:011d}"])
        self.assertEqual(b["plan_only_video_ids"], [f"{3:011d}", f"{5:011d}"])
        self.assertEqual(next(row for row in b["jobs"] if row["video_id"]==f"{4:011d}")["disposition"],
                         "HOLD_IN_PROGRESS")
        self.assertFalse(b["production_live_batch_authorized"])

    def test_bad_or_partial_media_never_enters_queue(self):
        for n in range(1,5):
            self._cache(n, video=(n != 2), subtitle=(n != 3))
        output = plan_research_batch(self.root, max_jobs=2)
        self.assertEqual(output["plan_only_video_ids"], [f"{1:011d}", f"{4:011d}"])
        self.assertEqual(output["disposition_counts"]["HOLD_MEDIA_INPUTS_INCOMPLETE"], 2)
        self.assertNotIn(str(self.root), json.dumps(output))

    def test_invalid_input_cache_and_source_ledger_fail_closed(self):
        self._cache(1)
        p = self.root / "data" / "videos" / f"{1:011d}" / "media_cache.json"
        p.write_text("{bad json", encoding="utf-8")
        result = plan_research_batch(self.root)
        self.assertEqual(result["jobs"][0]["disposition"], "HOLD_INVALID_MEDIA_CACHE")
        self.index_file.write_text('{"source_count": 77, "sources":[]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            plan_research_batch(self.root)
        self.assertFalse((self.root / "data" / "batch_plans").exists())

    def test_saved_plan_is_content_addressed_and_idempotent(self):
        self._cache(1)
        before = self.index_file.read_bytes()
        first = plan_research_batch(self.root, max_jobs=1, save=True)
        second = plan_research_batch(self.root, max_jobs=1, save=True)
        self.assertEqual(first, second)
        filepath = Path(first["saved_plan_path"])
        self.assertTrue(filepath.exists())
        self.assertTrue(filepath.name.startswith("batch_plan_"))
        self.assertEqual(before, self.index_file.read_bytes())
        self.assertEqual(len(list(filepath.parent.glob("batch_plan_*.json"))), 1)
        self.assertNotIn("saved_plan_path", json.loads(filepath.read_text(encoding="utf-8")))

    def test_duplicate_source_aliases_are_preserved_once_per_video(self):
        rows = [{
            "source_id": "S001", "source_ids": ["S001","S002"],
            "video_id": f"{1:011d}", "playlist_position": 1,
        }] + [{
            "source_id": f"S{i:03d}", "source_ids": [f"S{i:03d}"],
            "video_id": f"{i:011d}", "playlist_position": i,
        } for i in range(3,78)]
        self.index_file.write_text(json.dumps({
            "source_count":77,"sources":rows
        }), encoding="utf-8")
        self._cache(1)
        result = plan_research_batch(self.root, max_jobs=3)
        self.assertEqual(result["source_entries"], 77)
        self.assertEqual(result["unique_videos"], 76)
        self.assertEqual(result["jobs"][0]["source_ids"], ["S001","S002"])
        self.assertEqual(result["plan_only_video_ids"], [f"{1:011d}"])

    def test_limit_out_of_range_rejected(self):
        for limit in (0, 11, -2):
            with self.subTest(limit=limit), self.assertRaisesRegex(
                BatchPlanBlocked,"BATCH_MAX_JOBS_RANGE"
            ):
                plan_research_batch(self.root, max_jobs=limit)


if __name__ == "__main__":
    unittest.main()
