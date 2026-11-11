"""Phase 3: synthetic real-PNG evidence gate and no-false-approval tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image

from ytresearch.quality_gate import assess_research_run


class ResearchQualityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video_id = "00000000001"
        self.run_id = "quality_fixture"
        self.vdir = self.root / "data" / "videos" / self.video_id
        self.run = self.vdir / "runs" / self.run_id
        self.run.mkdir(parents=True)
        self.board_path = self.run / "board_01.png"
        Image.new("RGB", (128, 64), (20, 40, 60)).save(self.board_path)
        self._write("multimodal_checkpoint.json", {
            "video_id": self.video_id, "run_id": self.run_id, "status": "COMPLETED"
        })
        self._write("frames_manifest.json", {
            "frame_count": 1,
            "frames": [{
                "frame_id": "frame_001", "width": 128, "height": 64,
                "source_width": 128, "source_height": 64, "actual_sec": 0,
                "path": str(self.board_path),
            }],
            "roi_frame_count": 0, "roi_frames": [],
            "board_count": 1,
            "boards": [{
                "board_id": "board_01", "path": str(self.board_path),
                "panel_frame_ids": ["frame_001"], "panel_indices": [1],
                "sha256": hashlib.sha256(self.board_path.read_bytes()).hexdigest(),
            }],
        })
        self._write("tiered_analysis.json", {
            "scene_analyses": [{
                "scene_id": 1, "start_sec": 0, "end_sec": 1,
                "summary": "click", "evidence_ref": "board_01.png (PANEL #01)",
            }],
            "prompts_extracted": [{
                "label": "generated", "evidence_kind": "GENERATED_CONTENT",
                "verbatim_text": "영상 내용 주장: 87%",
                "evidence_ref": "board_01.png (PANEL #01)",
            }],
            "ui_settings_extracted": [],
        })
        self._write("verification.json", {
            "items": [{
                "item_id": 1, "final_status": "SELF_CHECKED",
                "ground_truth": None, "independent_verifier_invoked": False,
            }],
        })
        self._write("parsed_subtitles.json", {
            "valid_segment_count": 1,
            "segments": [{"id": "s000001", "text": "메뉴 클릭"}],
        }, parent=self.vdir)
        (self.run / "final_report.md").write_text("# Evidence\n", encoding="utf-8")

    def _write(self, name, data, parent=None):
        (parent or self.run).joinpath(name).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def _edit(self, name, update):
        file = self.run / name
        data = json.loads(file.read_text(encoding="utf-8"))
        update(data)
        self._write(name, data)

    def test_valid_structural_evidence_does_not_grant_factual_approval(self):
        result = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(result["structural_status"], "PASS")
        self.assertEqual(result["approval_status"], "REVIEW_REQUIRED")
        self.assertFalse(result["independent_content_accuracy_measured"])
        self.assertTrue(result["human_review_required"])
        self.assertEqual(result["metrics"]["validated_evidence_panel_refs"], 2)
        self.assertEqual(result["metrics"]["board_sha256_checked"], 1)
        self.assertTrue((self.run / "quality_gate.json").exists())
        self.assertEqual(result["generated_claim_observations"][0]["code"],
                         "SOURCE_CLAIM_UNVERIFIED")

    def test_tampered_png_blocks_gate(self):
        Image.new("RGB", (128, 64), (99, 99, 99)).save(self.board_path)
        report = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(report["structural_status"], "BLOCKED")
        self.assertTrue(any("BOARD_SHA256_MISMATCH" in x for x in report["errors"]))

    def test_nonexistent_panels_are_blocked(self):
        self._edit("tiered_analysis.json", lambda d:
                   d["prompts_extracted"][0].update(
                       evidence_ref="board_01.png (PANEL #99)"
                   ))
        report = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(report["structural_status"], "BLOCKED")
        self.assertTrue(any("INVALID_EVIDENCE_REF" in x for x in report["errors"]))

    def test_unsupported_verified_status_and_model_ground_truth_are_blocked(self):
        def change(doc):
            doc["items"][0].update(
                final_status="VERIFIED", ground_truth="self report"
            )
        self._edit("verification.json", change)
        result = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertIn("AMBIGUOUS_LEGACY_VERIFIED_STATUS", result["errors"])
        self.assertIn("MODEL_GROUND_TRUTH_WITHOUT_INDEPENDENT_PROVENANCE", result["errors"])
        self.assertEqual(result["approval_status"], "NOT_APPROVABLE")

    def test_missing_ui_ref_is_warning_not_false_accuracy_claim(self):
        self._edit("tiered_analysis.json", lambda doc:
                   doc["prompts_extracted"][0].pop("evidence_ref"))
        result = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(result["structural_status"], "PASS")
        self.assertIn("UNREFERENCED_SCREEN_TEXT_ITEMS:1", result["warnings"])

    def test_missing_subtitle_and_count_mismatch_block(self):
        (self.vdir / "parsed_subtitles.json").unlink()
        a = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(a["structural_status"], "BLOCKED")
        self.assertIn("SUBTITLE_SOURCE_UNAVAILABLE", a["errors"])

    def test_analysis_requires_verification_items(self):
        self._edit("verification.json", lambda x: x.update(items=[]))
        a = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertIn("MISSING_VERIFICATION_ITEMS", a["errors"])

    def test_no_manual_media_is_sent_to_github(self):
        self.assertFalse((self.root / "research" / "index.json").exists())
        report = assess_research_run(self.root, self.video_id, self.run_id, persist=False)
        self.assertEqual(report["structural_status"], "PASS")
        self.assertFalse((self.run / "quality_gate.json").exists())


    def test_p0_generated_model_claim_is_flagged_not_verified(self):
        self._edit("tiered_analysis.json", lambda doc:
                   doc.update(research_items=[{
                       "section_id": 15, "timestamp": "15:06",
                       "text": "무료 플랜 노트북 100개",
                       "claim_origin": "GENERATED_SLIDE",
                       "evidence_ref": "board_01.png (PANEL #01)",
                   }]))
        report = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(report["structural_status"], "PASS")
        self.assertEqual(report["semantic_evidence_status"], "REVIEW_REQUIRED")
        self.assertEqual(report["claim_review"]["claims_total"], 1)
        self.assertIn("SERVICE_QUOTA_UNVERIFIED",
                      report["claim_review"]["items"][0]["review_flags"])
        self.assertEqual(
            report["claim_review"]["items"][0]["evidence_content_match"], "NOT_REVIEWED"
        )
        self.assertFalse(report["independent_content_accuracy_measured"])

    def test_unsupported_model_origin_blocks_structurally_without_crash(self):
        self._edit("tiered_analysis.json", lambda doc:
                   doc.update(research_items=[{
                       "section_id": 15, "timestamp": "15:06",
                       "text": "모델 자체가 사실 검증한 요금제",
                       "claim_origin": "OFFICIAL_VERIFIED",
                       "evidence_ref": "board_01.png (PANEL #01)",
                   }]))
        report = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(report["structural_status"], "BLOCKED")
        self.assertTrue(any(x.startswith("INVALID_RESEARCH_CLAIM_PROVENANCE:")
                            for x in report["errors"]))

    def test_unproven_cli_and_workflow_actions_are_rejected(self):
        self._edit("tiered_analysis.json", lambda doc:
                   doc.update(workflow_steps=[{
                       "timestamp": "00:05", "action": "원클릭으로 실행됨",
                       "interaction_type": "UNKNOWN_ACTION",
                       "evidence_ref": "",
                   }], cli_commands=[{
                       "timestamp": "00:05", "command": "rm -rf /",
                       "evidence_ref": "",
                   }]))
        report = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(report["structural_status"], "BLOCKED")
        self.assertTrue(any("UNCLASSIFIED_WORKFLOW_INTERACTION" in x for x in report["errors"]))
        self.assertTrue(any("UNSOURCED_CLI_COMMANDS_ITEM" in x for x in report["errors"]))


    def test_generic_unverified_source_has_p2_without_false_p0_warning(self):
        self._edit("tiered_analysis.json", lambda doc:
                   doc.update(research_items=[{
                       "section_id": 1, "timestamp": "00:01",
                       "text": "사용자가 NotebookLM 화면을 살펴본다",
                       "evidence_ref": "board_01.png (PANEL #01)",
                   }]))
        result = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(result["structural_status"], "PASS")
        self.assertEqual(result["metrics"]["research_claim_priority_counts"],
                         {"P0": 0, "P1": 0, "P2": 1})
        self.assertEqual(result["metrics"]["research_claims_unverified"], 1)
        self.assertFalse(any("RESEARCH_CLAIM_RISK:" in warning for warning in result["warnings"]))
        self.assertTrue(result["human_review_required"])
        self.assertEqual(result["semantic_evidence_status"], "REVIEW_REQUIRED")

    def test_specific_quota_risk_warns_p0_but_does_not_block_structure(self):
        self._edit("tiered_analysis.json", lambda doc:
                   doc.update(research_items=[{
                       "section_id": 15, "timestamp": "00:01",
                       "text": "무료 플랜 노트북 100개 유료 플랜 500개",
                       "claim_origin": "GENERATED_SLIDE",
                       "evidence_ref": "board_01.png (PANEL #01)",
                   }]))
        result = assess_research_run(self.root, self.video_id, self.run_id)
        self.assertEqual(result["structural_status"], "PASS")
        self.assertEqual(result["metrics"]["research_claim_priority_counts"],
                         {"P0": 1, "P1": 0, "P2": 0})
        self.assertTrue(any("RESEARCH_CLAIM_RISK:SERVICE_QUOTA_UNVERIFIED:1"
                            in warning for warning in result["warnings"]))
        self.assertEqual(result["approval_status"], "REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
