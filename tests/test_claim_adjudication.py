"""Audit-derived dispositions are distinct from P0/P1/P2 and never model facts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ytresearch.claim_adjudication import (
    ClaimAuditInvalid, import_claim_audit, summarize_audited_claims,
    validate_audit_document,
)


VIDEO = "Tst00000002"
RUN = "argon_b2x32_c1bd5131"
INDICES = (2, 3, 5, 6, 7, 9)
DECISIONS = {
    2: ("UNVERIFIED", "PARTIAL"),
    3: ("UNVERIFIED", "PARTIAL"),
    5: ("UNVERIFIED", "PARTIAL"),
    6: ("CONFIRMED_ERROR", "PARTIAL"),
    7: ("SOURCE_CONFLICT", "CONFLICT"),
    9: ("UNVERIFIED", "PARTIAL"),
}


class ClaimAdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "data" / "videos" / VIDEO / "runs" / RUN
        self.run_dir.mkdir(parents=True)
        self.vdir = self.run_dir.parent.parent
        self.analysis_file = self.run_dir / "tiered_analysis.json"
        self.manifest_file = self.run_dir / "frames_manifest.json"
        self.subtitle_file = self.vdir / "parsed_subtitles.json"
        self.analysis = {
            "research_items": [
                {"section_id": section, "text": f"Audit fixture statement {i}",
                 "timestamp": "00:01", "evidence_ref": "board_01.png (PANEL #01)"}
                for i, section in enumerate((1,2,9,10,14,15,16,17,18), 1)
            ]
        }
        self.manifest = {
            "boards": [{
                "board_id": "board_01", "path": "C:/tmp/board_01.png",
                "panel_frame_ids": ["frame_001"], "panel_indices": [1],
                "timestamps_sec": [1.0],
            }],
        }
        self.subtitles = {
            "segments": [{"id": "s000001", "text": "illustrative subtitle"}],
            "valid_segment_count": 1,
        }
        self._save()
        self.audit_file = self.root / "audit.json"
        self.audit = self._build_audit()
        self.audit_file.write_text(json.dumps(self.audit, ensure_ascii=False), encoding="utf-8")

    def _save(self):
        self.analysis_file.write_text(json.dumps(self.analysis, ensure_ascii=False), encoding="utf-8")
        self.manifest_file.write_text(json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")
        self.subtitle_file.write_text(json.dumps(self.subtitles, ensure_ascii=False), encoding="utf-8")

    def _build_audit(self):
        from ytresearch.claim_adjudication import IMPORT_ORIGIN
        doc = {
            "schema_version": 1, "audit_id": "six-manual-review-example",
            "audit_origin": IMPORT_ORIGIN,
            "video_id": VIDEO, "run_id": RUN,
            "source_analysis_sha256": hashlib.sha256(self.analysis_file.read_bytes()).hexdigest(),
            "source_frames_manifest_sha256": hashlib.sha256(self.manifest_file.read_bytes()).hexdigest(),
            "source_subtitles_sha256": hashlib.sha256(self.subtitle_file.read_bytes()).hexdigest(),
            "findings": [],
        }
        for i in INDICES:
            claim = self.analysis["research_items"][i-1]
            status, match = DECISIONS[i]
            doc["findings"].append({
                "item_index": i, "section_id": claim["section_id"],
                "claim_sha256": hashlib.sha256(claim["text"].encode("utf-8")).hexdigest(),
                "finding_status": status, "source_match": match,
                "trigger": "RULE_BASED_TEST_TRIGGER",
                "review_note": "Manually reviewed source-claim relationship; this is a synthetic test input.",
                "correction": "A separate documented editorial correction is required before approval.",
                "evidence_refs": [claim["evidence_ref"]],
                "subtitle_ids": ["s000001"],
                "external_sources": ["https://example.org/help"] if i == 6 else [],
            })
        return doc

    def _write_audit(self):
        self.audit_file.write_text(json.dumps(self.audit, ensure_ascii=False), encoding="utf-8")

    def test_six_findings_leave_three_as_not_reviewed(self):
        result = import_claim_audit(self.root, self.audit_file)
        self.assertEqual(result["status_counts"], {
            "CONFIRMED_ERROR": 1, "SOURCE_CONFLICT": 1,
            "UNVERIFIED": 4, "NOT_REVIEWED": 3,
        })
        self.assertEqual(result["unreviewed_indices"], [1,4,8])
        summary = summarize_audited_claims(self.root, VIDEO, RUN)
        self.assertEqual(summary["status_counts"], result["status_counts"])
        self.assertTrue(summary["has_unresolved_confirmed_problem"])
        self.assertEqual(summary["items"][5]["finding_status"], "CONFIRMED_ERROR")
        self.assertEqual(summary["items"][6]["finding_status"], "SOURCE_CONFLICT")
        self.assertEqual(summary["items"][0]["finding_status"], "NOT_REVIEWED")
        self.assertFalse(summary["automatic_factual_verification"])

    def test_source_bytes_preserved_and_import_is_idempotent(self):
        original = [p.read_bytes() for p in (
            self.analysis_file, self.manifest_file, self.subtitle_file,
        )]
        first = import_claim_audit(self.root, self.audit_file)
        second = import_claim_audit(self.root, self.audit_file)
        self.assertEqual(first["status_counts"], second["status_counts"])
        self.assertEqual(original, [p.read_bytes() for p in (
            self.analysis_file, self.manifest_file, self.subtitle_file,
        )])
        self.assertEqual(len(list(self.run_dir.glob("claim_adjudications.json"))), 1)
        self.assertFalse((self.run_dir / "research_report_19.md").exists())

    def test_tampered_source_fails_closed(self):
        import_claim_audit(self.root, self.audit_file)
        self.analysis["research_items"][5]["text"] = "mutated source"
        self._save()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_SOURCE_HASH_MISMATCH"):
            summarize_audited_claims(self.root, VIDEO, RUN)

    def test_no_automatic_inference_without_import(self):
        data = summarize_audited_claims(self.root, VIDEO, RUN)
        self.assertEqual(data["status_counts"]["NOT_REVIEWED"], 9)
        self.assertEqual(data["reviewed_claims"], 0)
        self.assertIsNone(data["audit_id"])

    def test_forged_content_or_section_is_rejected(self):
        self.audit["findings"][0]["claim_sha256"] = "0"*64
        self._write_audit()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_CLAIM_HASH_MISMATCH"):
            import_claim_audit(self.root, self.audit_file)
        self.audit = self._build_audit()
        self.audit["findings"][0]["section_id"] = 17
        self._write_audit()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_SECTION_MISMATCH"):
            import_claim_audit(self.root, self.audit_file)

    def test_forged_panel_or_subtitle_is_rejected(self):
        self.audit["findings"][0]["evidence_refs"] = ["board_01.png (PANEL #99)"]
        self._write_audit()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_REF_NOT_IN_ORIGINAL_CLAIM"):
            import_claim_audit(self.root, self.audit_file)
        self.audit = self._build_audit()
        self.audit["findings"][0]["subtitle_ids"] = ["s999999"]
        self._write_audit()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_SUBTITLE_ID_MISSING"):
            import_claim_audit(self.root, self.audit_file)

    def test_confirmed_error_must_include_source_and_conflict_must_be_supported(self):
        self.audit["findings"][3]["external_sources"] = []
        self._write_audit()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_CONFIRMED_ERROR_WITHOUT_EXTERNAL_SOURCE"):
            import_claim_audit(self.root, self.audit_file)
        self.audit = self._build_audit()
        self.audit["findings"][4]["source_match"] = "PARTIAL"
        self._write_audit()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_CONFLICT_WITHOUT_SOURCE_CONFLICT"):
            import_claim_audit(self.root, self.audit_file)

    def test_different_import_cannot_overwrite_existing_audit(self):
        import_claim_audit(self.root, self.audit_file)
        self.audit["findings"][0]["review_note"] += " Updated after import."
        self._write_audit()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_SIDECAR_ALREADY_PRESENT_NO_OVERWRITE"):
            import_claim_audit(self.root, self.audit_file)

    def test_hash_mismatch_before_any_mutation(self):
        self.audit["source_frames_manifest_sha256"] = "0"*64
        self._write_audit()
        with self.assertRaisesRegex(ClaimAuditInvalid, "AUDIT_SOURCE_HASH_MISMATCH"):
            import_claim_audit(self.root, self.audit_file)
        self.assertFalse((self.run_dir / "claim_adjudications.json").exists())


if __name__ == "__main__":
    unittest.main()
