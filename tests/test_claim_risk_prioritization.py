"""Nine audit-derived V3.4 claims: risk priority vs verification gap.

Text excerpts preserve the content-specific triggers in the real NotebookLM
V3.4 research_items but are not declarations of factual truth. No model/media
calls; the fixture represents the pre-V3.5 absence of claim_origin.
"""
from __future__ import annotations
import unittest
import json
import tempfile
from pathlib import Path
from ytresearch.claim_review import (
    review_research_claim, summarize_claim_reviews, review_cached_research_claims,
)


def manifest():
    # The real V3.4 evidence boards display global PANEL indices. Nonlocal
    # citations in a multi-scene synthesis are not in themselves errors.
    times = {
        1: (21.967, 59.967), 2: (98, 141.967),
        5: (289.967, 319.967), 6: (357.967, 392),
        7: (423.967, 454), 8: (483.967, 499.967),
        9: (545.967, 589.967), 10: (614, 644),
        11: (674, 705), 12: (735, 767.967),
        13: (797.967, 833.967), 14: (875, 906),
        15: (941.967, 978),
    }
    boards = []
    for n in range(1, 17):
        a,b = times.get(n, (n*64.0, n*64.0+20))
        boards.append({
            "board_id": f"board_{n:02d}",
            "path": f"C:/runs/board_{n:02d}.png",
            "panel_frame_ids": [f"frame_{2*n-1:03d}", f"frame_{2*n:03d}"],
            "timestamps_sec": [a,b],
        })
    return {"boards": boards}


# Structural evidence and original section numbers from the 9 audited V3.4
# entries; deliberately no claim_origin (legacy metadata was absent).
NINE_CLAIMS = [
    (1,  "00:21", "NotebookLM 인포그래픽·슬라이드 덱, Fast/Deep Research와 스튜디오 기능 업데이트",
     "board_01.png (PANEL #01), board_09.png (PANEL #17)"),
    (2,  "07:34", "음성 발화에서 Gemini 1.5 모델을 언급하고 생성 화면에는 Nano Banana Pro가 표기됨",
     "board_01.png (PANEL #02), board_07.png (PANEL #14), board_13.png (PANEL #26)"),
    (9,  "02:37", "소스 기반 RAG와 인라인 인용으로 일반 LLM 대비 환각을 억제한다는 설명",
     "board_02.png (PANEL #03), board_07.png (PANEL #14), board_08.png (PANEL #16)"),
    (10, "05:12", "Fast Research에서 reddit 최근 3개월 자료를 요청하고 스타일 입력·간결하게를 지정",
     "board_06.png (PANEL #11), board_12.png (PANEL #24), board_15.png (PANEL #29)"),
    (14, "06:20", "여러 수동 단계를 거쳐 원클릭 발표 자료 생성까지 이어지는 워크플로우",
     "board_06.png (PANEL #11), board_07.png (PANEL #13), board_15.png (PANEL #29)"),
    (15, "15:06", "무료 플랜 월간 노트북 100개·소스 50개·일일 채팅 50회, Plus 500개·소스 300개",
     "board_08.png (PANEL #16), board_10.png (PANEL #20), board_14.png (PANEL #28)"),
    (16, "04:53", "일반 LLM의 환각 위험과 NotebookLM의 소스 기반 인용 비교",
     "board_05.png (PANEL #09), board_11.png (PANEL #22)"),
    (17, "12:47", "유료 로그인 웹사이트의 수집 제한과 생성 이미지의 작은 한글 오타 주의",
     "board_07.png (PANEL #14), board_12.png (PANEL #24)"),
    (18, "10:44", "코드 레포지토리를 499,999단어 이하의 .txt 파일 조각으로 분할 업로드해 한도를 우회하는 전략",
     "board_08.png (PANEL #16), board_10.png (PANEL #20)"),
]


def analysis():
    return {"research_items": [
        {"section_id": sec, "timestamp": stamp, "text": body,
         "evidence_ref": evidence}
        for sec, stamp, body, evidence in NINE_CLAIMS
    ]}


class RiskSeparationV34Tests(unittest.TestCase):
    def test_all_nine_split_into_two_p0_four_p1_three_p2(self):
        summary = summarize_claim_reviews(analysis(), manifest())
        self.assertEqual(summary["claims_total"], 9)
        self.assertEqual(summary["priority_counts"], {"P0": 2, "P1": 4, "P2": 3})
        self.assertEqual(summary["high_priority_claims"], 2)
        self.assertEqual(summary["unverified_claims"], 9)
        self.assertEqual(summary["claims_with_content_specific_risk"], 6)
        self.assertEqual(summary["claims_only_general_review"], 3)
        self.assertEqual(summary["independent_semantic_checks_completed"], 0)
        self.assertTrue(all(
            row["factual_verification_status"] == "UNVERIFIED"
            and row["evidence_content_match"] == "NOT_REVIEWED"
            and row["claim_origin"] == "LEGACY_UNATTRIBUTED"
            for row in summary["items"]
        ))
        self.assertEqual(
            [r["review_priority"] for r in summary["items"]],
            ["P2", "P1", "P1", "P2", "P1", "P0", "P1", "P2", "P0"],
        )

    def test_generic_legacy_status_never_triggers_p0(self):
        report = summarize_claim_reviews({
            "research_items": [analysis()["research_items"][i] for i in (0,3,7)]
        }, manifest())
        self.assertEqual(report["priority_counts"], {"P0":0,"P1":0,"P2":3})
        for item in report["items"]:
            self.assertEqual(item["risk_signals"], [])
            self.assertIn("LEGACY_SOURCE_TYPE_UNATTRIBUTED", item["verification_gaps"])
            self.assertIn("EVIDENCE_CONTENT_MATCH_NOT_REVIEWED", item["verification_gaps"])

    def test_temporally_distant_secondary_citations_are_context_only(self):
        row = summarize_claim_reviews({"research_items": [
            analysis()["research_items"][0],
        ]}, manifest())["items"][0]
        self.assertIn("CROSS_TIME_COMPOSITE_REFERENCE", row["context_flags"])
        self.assertNotIn("CROSS_TIME_COMPOSITE_REFERENCE", row["risk_signals"])
        self.assertEqual(row["review_priority"], "P2")

    def test_all_citations_far_from_event_become_targeted_not_p0(self):
        entry = {"section_id": 1, "text":"NotebookLM 화면 메뉴 소개",
                 "timestamp":"01:20", "evidence_ref":"board_14.png (PANEL #28)"}
        result = review_research_claim(entry, manifest())
        self.assertEqual(result["review_priority"], "P1")
        self.assertIn("ALL_CITED_PANELS_TEMPORALLY_DISTANT", result["risk_signals"])

    def test_low_risk_unverified_generated_content_stays_p2(self):
        claim = {"section_id": 1, "claim_origin":"GENERATED_SLIDE",
                 "text":"테마 소개를 위한 자동 생성 이미지",
                 "timestamp":"15:06", "evidence_ref":"board_14.png (PANEL #28)"}
        result = review_research_claim(claim, manifest())
        self.assertEqual(result["review_priority"], "P2")
        self.assertIn("SOURCE_CLAIM_UNVERIFIED", result["verification_gaps"])
        self.assertEqual(result["risk_signals"], [])

    def test_number_only_or_generic_bypass_word_is_not_unproven_workaround(self):
        for txt in ("499,999", "일반적인 우회 설명", "코드베이스 백업 파일"):
            with self.subTest(txt=txt):
                claim = {"section_id": 18, "text": txt, "timestamp":"10:44",
                         "evidence_ref":"board_10.png (PANEL #20)"}
                result = review_research_claim(claim, manifest())
                self.assertEqual(result["review_priority"], "P2")
                self.assertNotIn("UNTESTED_WORKAROUND", result["review_flags"])

    def test_specific_p0_signals_still_trigger_without_source_type(self):
        rows = analysis()["research_items"]
        for claim, expected in ((rows[5],"SERVICE_QUOTA_UNVERIFIED"),
                                (rows[8],"UNTESTED_WORKAROUND")):
            with self.subTest(expected=expected):
                result = review_research_claim(claim, manifest())
                self.assertEqual(result["review_priority"], "P0")
                self.assertIn(expected, result["risk_signals"])
                self.assertNotIn("SOURCE_CLAIM_UNVERIFIED", result["risk_signals"])

    def test_mixed_sources_alone_not_automatically_critical(self):
        row = {"section_id": 1, "text":"다양한 화면 및 발화 종합",
               "claim_origin":"MIXED_SOURCES", "timestamp":"00:21",
               "evidence_ref":"board_01.png (PANEL #01)"}
        result = review_research_claim(row, manifest())
        self.assertEqual(result["review_priority"], "P1")
        self.assertIn("MIXED_CLAIMS_REQUIRE_SPLIT", result["risk_signals"])


    def test_local_existing_v34_files_are_not_modified_by_offline_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vid, rid = "Tst00000002", "argon_b2x32_c1bd5131"
            folder = root / "data" / "videos" / vid / "runs" / rid
            folder.mkdir(parents=True)
            analysis_file = folder / "tiered_analysis.json"
            manifest_file = folder / "frames_manifest.json"
            analysis_file.write_text(json.dumps(analysis(), ensure_ascii=False), encoding="utf-8")
            manifest_file.write_text(json.dumps(manifest(), ensure_ascii=False), encoding="utf-8")
            original_analysis = analysis_file.read_bytes()
            original_manifest = manifest_file.read_bytes()
            result = review_cached_research_claims(root, vid, rid)
            self.assertEqual(result["priority_counts"], {"P0":2,"P1":4,"P2":3})
            self.assertEqual(result["unverified_claims"], 9)
            self.assertEqual(result["independent_semantic_checks_completed"], 0)
            self.assertTrue(Path(result["output_path"]).is_file())
            self.assertEqual(analysis_file.read_bytes(), original_analysis)
            self.assertEqual(manifest_file.read_bytes(), original_manifest)
            saved = json.loads(Path(result["output_path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["priority_counts"], {"P0":2,"P1":4,"P2":3})
            self.assertEqual(saved["provenance"], "DETERMINISTIC_OFFLINE_RECLASSIFICATION")
            self.assertFalse((folder / "research_report_19.md").exists())
            rerun = review_cached_research_claims(root, vid, rid, persist=False)
            self.assertEqual(rerun["priority_counts"], result["priority_counts"])
            self.assertNotIn("output_path", rerun)


if __name__ == "__main__":
    unittest.main()
