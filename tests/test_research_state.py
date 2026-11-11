"""Phase 1: source-to-run state migration, strict status, and approval tests."""
from __future__ import annotations
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from ytresearch.research_state import (
    open_research_db, read_source_ledger, sync_research_sources,
    record_analysis_checkpoint, scan_local_runs, snapshot_research_state,
    update_report_review,
)


class ResearchStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "research").mkdir()
        self.rows = [
            {"source_id": f"S{i:03d}", "source_ids": [f"S{i:03d}"],
             "video_id": f"{i:011d}", "playlist_position": i,
             "status": "not_started", "manifest": None, "gpt_report": None}
            for i in range(1, 78)
        ]
        self.index = self.root / "research" / "index.json"
        self.index.write_text(json.dumps({
            "source_count": 77, "source_entries_count": 77, "sources": self.rows
        }, ensure_ascii=False), encoding="utf-8")

    def _completed_run(self, vid="00000000001", run_id="argon_v3_sample"):
        path = self.root / "data" / "videos" / vid / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        ckpt = {
            "video_id": vid, "run_id": run_id, "status": "COMPLETED",
            "updated_utc": "2026-11-10T08:00:00+00:00",
            "last_completed_stage": "markdown_assembled",
            "run_config": {"primary_model": "gemini-4-argon-high"},
            "stages": {"markdown_assembled": {"status": "DONE"}},
        }
        (path / "multimodal_checkpoint.json").write_text(json.dumps(ckpt), encoding="utf-8")
        for name in ("frames_manifest.json", "tiered_analysis.json", "verification.json"):
            (path / name).write_text("{}", encoding="utf-8")
        (path / "final_report.md").write_text("# Draft\n", encoding="utf-8")
        return path

    def test_source_import_idempotent_and_preserves_tracked_index(self):
        original = self.index.read_bytes()
        a = sync_research_sources(self.root)
        b = sync_research_sources(self.root)
        self.assertEqual(a, b)
        self.assertEqual(a, {"source_entries": 77, "unique_videos": 77})
        self.assertEqual(self.index.read_bytes(), original)
        self.assertEqual(read_source_ledger(self.root)["entry_count"], 77)

    def test_scan_existing_v3_checkpoints_and_track_evidence_draft(self):
        sync_research_sources(self.root)
        self._completed_run()
        summary = scan_local_runs(self.root)
        self.assertEqual(len(summary["imported_runs"]), 1)
        self.assertEqual(summary["errors"], [])
        row = summary["imported_runs"][0]
        self.assertEqual(row["status"], "COMPLETED")
        snapshot = snapshot_research_state(self.root)
        self.assertEqual(snapshot["summary"]["analyzed_videos"], 1)
        self.assertEqual(snapshot["summary"]["approved_research_videos"], 0)
        self.assertEqual(snapshot["sources"][0]["latest_run_id"], "argon_v3_sample")
        self.assertEqual(snapshot["sources"][0]["analysis_status"], "COMPLETED")
        self.assertTrue((self.root / "data" / "research_state_snapshot.json").exists())
        with closing(open_research_db(self.root)) as db:
            row = db.execute("SELECT review_state FROM reports").fetchone()
            self.assertEqual(row["review_state"], "REVIEW_REQUIRED")
        scan_local_runs(self.root)
        with closing(open_research_db(self.root)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 1)

    def test_explicit_human_review_and_publication_only(self):
        sync_research_sources(self.root)
        self._completed_run()
        scan_local_runs(self.root)
        with self.assertRaisesRegex(ValueError, "APPROVAL_REQUIRED"):
            update_report_review(self.root, "00000000001", "argon_v3_sample",
                                 kind="EVIDENCE", next_state="PUBLISHED")
        with self.assertRaisesRegex(ValueError, "EXPLICIT_REVIEWER"):
            update_report_review(self.root, "00000000001", "argon_v3_sample",
                                 kind="EVIDENCE", next_state="APPROVED")
        update_report_review(self.root, "00000000001", "argon_v3_sample",
                             kind="EVIDENCE", next_state="APPROVED", reviewer="human-reviewed")
        scan_local_runs(self.root)
        with closing(open_research_db(self.root)) as db:
            self.assertEqual(db.execute("SELECT review_state FROM reports").fetchone()[0], "APPROVED")
        update_report_review(self.root, "00000000001", "argon_v3_sample",
                             kind="EVIDENCE", next_state="PUBLISHED")
        with self.assertRaisesRegex(ValueError, "REVIEW_DOWNGRADE"):
            update_report_review(self.root, "00000000001", "argon_v3_sample",
                                 kind="EVIDENCE", next_state="DRAFT")

    def test_completed_checkpoint_missing_artifact_marked_inconsistent(self):
        sync_research_sources(self.root)
        path = self._completed_run()
        (path / "verification.json").unlink()
        result = record_analysis_checkpoint(self.root, "00000000001", "argon_v3_sample")
        self.assertEqual(result["status"], "INCONSISTENT")
        self.assertIn("verification.json", result["missing_artifacts"])

    def test_source_id_reassignment_is_blocked_without_original_mutation(self):
        sync_research_sources(self.root)
        previous = self.index.read_bytes()
        data = json.loads(previous)
        data["sources"][0]["video_id"] = "11111111111"
        self.index.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SOURCE_ID_REASSIGNMENT"):
            sync_research_sources(self.root)
        with closing(open_research_db(self.root)) as db:
            self.assertEqual(db.execute(
                "SELECT video_id FROM source_entries WHERE source_id='S001'"
            ).fetchone()[0], "00000000001")

    def test_reject_non_registered_checkpoint_or_conflicting_id(self):
        sync_research_sources(self.root)
        self._completed_run()
        with self.assertRaisesRegex(ValueError, "UNREGISTERED_VIDEO"):
            # Input isn't a registered ID, even if a run file were available.
            bad = self._completed_run("99999999999")
            record_analysis_checkpoint(self.root, "99999999999", "argon_v3_sample")
        path = self._completed_run()
        ckpt_path = path / "multimodal_checkpoint.json"
        obj = json.loads(ckpt_path.read_text())
        obj["video_id"] = "99999999999"
        ckpt_path.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ValueError, "CHECKPOINT_ID_MISMATCH"):
            record_analysis_checkpoint(self.root, "00000000001", "argon_v3_sample")


if __name__ == "__main__":
    unittest.main()
