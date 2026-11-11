"""P0/P1 regression: reproduce NotebookLM V3.4 source-layer and citation defects.

The fixtures are short, representative snippets from the user-supplied audit.
They are not independently verified technical claims, ground truth, or model
performance measurements.
"""
from __future__ import annotations

import unittest

from ytresearch.claim_review import (
    board_timestamps_for_ref, review_research_claim, summarize_claim_reviews,
)


def evidence_manifest():
    boards = []
    for n in range(1, 17):
        first = 2*n-1
        times = [float(first*32), float(first*32+10)]
        if n == 1:
            times = [21.967, 55]
        elif n == 9:
            times = [545.967, 589.967]
        elif n == 10:
            times = [614, 644]
        elif n == 13:
            times = [797, 833.967]
        elif n == 14:
            times = [875, 906]
        boards.append({
            "board_id": f"board_{n:02d}",
            "path": f"C:\\runs\\boards\\board_{n:02d}.png",
            "panel_frame_ids": [f"frame_{first:03d}", f"frame_{first+1:03d}"],
            "timestamps_sec": times,
        })
    return {"boards": boards}


def item(section, text, time, ref, origin=None):
    result = {
        "section_id": section, "text": text, "timestamp": time,
        "evidence_ref": ref, "source_segment_ids": [],
    }
    if origin:
        result["claim_origin"] = origin
    return result


class V34ClaimAuditRegressions(unittest.TestCase):
    def setUp(self):
        self.manifest = evidence_manifest()

    def test_p0_generated_slide_quota_remains_unverified(self):
        claim = item(15, "무료 플랜은 월간 노트북 100개, 유료 플랜 500개를 제공함",
                     "15:06", "board_14.png (PANEL #28)", "GENERATED_SLIDE")
        result = review_research_claim(claim, self.manifest)
        self.assertEqual(result["review_priority"], "P0")
        self.assertIn("SERVICE_QUOTA_UNVERIFIED", result["review_flags"])
        self.assertIn("SOURCE_CLAIM_UNVERIFIED", result["review_flags"])
        self.assertEqual(result["factual_verification_status"], "UNVERIFIED")
        self.assertEqual(result["evidence_content_match"], "NOT_REVIEWED")

    def test_p0_ai_chat_workaround_not_promoted_to_proven_strategy(self):
        claim = item(18,
            "수천 개 코드 레포지토리를 499,999단어 파일 조각으로 분할해 한도를 우회",
            "10:44", "board_10.png (PANEL #20)", "AI_GENERATED_OUTPUT")
        result = review_research_claim(claim, self.manifest)
        self.assertEqual(result["review_priority"], "P0")
        self.assertIn("UNTESTED_WORKAROUND", result["review_flags"])
        self.assertIn("SOURCE_CLAIM_UNVERIFIED", result["review_flags"])

    def test_p1_model_name_is_not_proof_of_active_model(self):
        claim = item(2, "Gemini 1.5와 Nano Banana Pro 사용 모델 설명",
                     "08:39", "board_13.png (PANEL #26)", "SPEAKER_STATEMENT")
        result = review_research_claim(claim, self.manifest)
        self.assertIn("MODEL_VERSION_ATTRIBUTION_UNVERIFIED", result["review_flags"])
        self.assertEqual(result["factual_verification_status"], "UNVERIFIED")

    def test_p1_model_architecture_and_accuracy_are_not_proven(self):
        for section in (9, 16):
            with self.subTest(section=section):
                claim = item(section,
                    "RAG를 활용해 소스에만 근거한 응답으로 환각을 억제",
                    "02:37", "board_02.png (PANEL #03)", "SPEAKER_STATEMENT")
                flags = review_research_claim(claim, self.manifest)["review_flags"]
                self.assertIn("ARCHITECTURE_OR_ACCURACY_UNVERIFIED", flags)

    def test_p1_one_click_is_not_a_reproducible_manual_workflow(self):
        claim = item(14, "원클릭 발표 자료 생성 워크플로우",
                     "06:20", "board_09.png (PANEL #17)", "MIXED_SOURCES")
        result = review_research_claim(claim, self.manifest)
        self.assertIn("WORKFLOW_STEPS_UNVERIFIED", result["review_flags"])
        self.assertIn("MIXED_CLAIMS_REQUIRE_SPLIT", result["review_flags"])

    def test_p1_structurally_valid_pdfs_cannot_be_semantically_certified(self):
        claim = item(1, "NotebookLM Studio 업데이트 기능 소개",
                     "00:21", "board_01.png (PANEL #01), board_09.png (PANEL #17)")
        result = review_research_claim(claim, self.manifest)
        self.assertEqual(result["evidence_reference_status"], "STRUCTURALLY_VALID_ONLY")
        self.assertEqual(result["evidence_content_match"], "NOT_REVIEWED")
        self.assertIn("CROSS_TIME_COMPOSITE_REFERENCE", result["review_flags"])
        self.assertIn("board_09.png PANEL #17", result["distant_evidence_refs"])

    def test_legacy_v34_missing_origin_requires_review_not_inference(self):
        legacy = item(18, "사내 Q&A와 코드 데이터 분석 전략 가능",
                      "10:44", "board_10.png (PANEL #20)")
        review = review_research_claim(legacy, self.manifest)
        self.assertEqual(review["claim_origin"], "LEGACY_UNATTRIBUTED")
        self.assertIn("LEGACY_SOURCE_TYPE_UNATTRIBUTED", review["review_flags"])
        self.assertTrue(review["semantic_review_required"])

    def test_never_trust_self_declared_official_or_verified_origin(self):
        for origin in ("OFFICIAL_VERIFIED", "FACT_CHECKED", "FAKE_REVIEW"):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, "INVALID_CLAIM_ORIGIN"):
                review_research_claim(item(15, "요금제 100개", "15:06",
                                           "board_14.png (PANEL #28)", origin), self.manifest)

    def test_batch_summary_retains_every_audit_finding(self):
        claims = [
            item(15, "무료 플랜 100개 유료 플랜 500개", "15:06",
                 "board_14.png (PANEL #28)", "GENERATED_SLIDE"),
            item(18, "499,999단어 파일로 분할 업로드해 한도 우회", "10:44",
                 "board_10.png (PANEL #20)", "AI_GENERATED_OUTPUT"),
            item(14, "원클릭 발표자료 자동화", "06:20",
                 "board_09.png (PANEL #17)", "MIXED_SOURCES"),
        ]
        report = summarize_claim_reviews({"research_items": claims}, self.manifest)
        self.assertEqual(report["claims_total"], 3)
        self.assertEqual(report["independent_semantic_checks_completed"], 0)
        self.assertEqual(report["high_priority_claims"], 2)
        self.assertEqual(report["priority_counts"], {"P0": 2, "P1": 1, "P2": 0})
        self.assertEqual(report["unverified_claims"], 3)
        self.assertEqual(report["claims_with_content_specific_risk"], 3)
        self.assertEqual(report["flag_counts"]["SERVICE_QUOTA_UNVERIFIED"], 1)
        self.assertEqual(report["flag_counts"]["UNTESTED_WORKAROUND"], 1)
        self.assertEqual(report["flag_counts"]["WORKFLOW_STEPS_UNVERIFIED"], 1)
        self.assertTrue(all(r["evidence_content_match"] == "NOT_REVIEWED"
                            for r in report["items"]))

    def test_manifest_panel_timestamp_is_actual_rendered_id(self):
        frames = board_timestamps_for_ref("board_14.png (PANEL #28)", self.manifest)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["timestamp_sec"], 906)
        self.assertEqual(frames[0]["frame_id"], "frame_028")


if __name__ == "__main__":
    unittest.main()
