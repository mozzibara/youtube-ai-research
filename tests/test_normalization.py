"""Tests for lossless normalization of the actual category-headed AGY shape."""
import contextlib
import io
import json
import unittest

from ytresearch.agent import parse_structured_response
from ytresearch.definitions import CLAIM_SCHEMA
from ytresearch.normalization import normalize_category_claims


class NormalizationTests(unittest.TestCase):
    def make_response(self):
        count_by_heading = {
            "1. 핵심 AI 주제": 1,
            "2. AI 모델 및 서비스": 3,
            "8. Framework / Library": 1,
            "12. Automation": 1,
            "13. CLI 명령어 및 설정": 1,
            "14. 실전 Workflow": 2,
            "17. 실패 사례 / 안티패턴 / 주의사항": 2,
        }
        index = 0
        result = {}
        for title, count in count_by_heading.items():
            result[title] = []
            for _ in range(count):
                index += 1
                result[title].append({
                    "claim": f"evidence {index}",
                    "segment_ids": [f"s{index:06d}"],
                })
        return result

    def test_identical_repeated_documents_produce_eleven_claims(self):
        document = self.make_response()
        response = "\n".join(json.dumps(document, ensure_ascii=False) for _ in range(4))
        envelope = json.dumps({"status": "SUCCESS", "num_turns": 4, "response": response})
        data, usage = parse_structured_response(envelope, "", CLAIM_SCHEMA)
        self.assertEqual(len(data["claims"]), 11)
        self.assertEqual(usage["response_json_documents"], 4)
        self.assertEqual(usage["normalized_from"], "category_headings")
        self.assertEqual(usage["kind_default"], "source_claim")
        self.assertEqual({x["section"] for x in data["claims"]}, {1, 2, 8, 12, 13, 14, 17})
        self.assertTrue(all(x["kind"] == "source_claim" for x in data["claims"]))
        self.assertEqual(data["claims"][0]["text"], "evidence 1")
        self.assertEqual(data["claims"][0]["segment_ids"], ["s000001"])

    def test_response_content_remains_unchanged(self):
        document = self.make_response()
        normalized, changed = normalize_category_claims(document, CLAIM_SCHEMA)
        self.assertTrue(changed)
        self.assertEqual(
            {x["text"] for x in normalized["claims"]},
            {e["claim"] for items in document.values() for e in items}
        )

    def test_unknown_section_is_not_silently_accepted(self):
        result, changed = normalize_category_claims(
            {"999. unknown": [{"claim": "X", "segment_ids": ["s000001"]}]},
            CLAIM_SCHEMA
        )
        self.assertFalse(changed)
        self.assertIn("999. unknown", result)

    def test_missing_evidence_rejected(self):
        for record in ({"claim": "X", "segment_ids": []}, {"claim": "X"},
                       {"claim": "", "segment_ids": ["s000001"]}):
            with self.subTest(record=record):
                with self.assertRaisesRegex(RuntimeError, "AGY_CATEGORY_SHAPE"):
                    normalize_category_claims({"1. 핵심 AI 주제": [record]}, CLAIM_SCHEMA)

    def test_string_section_numbers_and_exact_headings_are_normalized(self):
        document = {"claims": [
            {"section": "13", "text": "명령어", "segment_ids": ["s000001"], "kind": "source_claim"},
            {"section": "8. Framework / Library", "text": "라이브러리", "segment_ids": ["s000002"], "kind": "source_claim"},
            {"section": 4, "text": "에이전트", "segment_ids": ["s000003"], "kind": "source_claim"},
        ]}
        result, changed = normalize_category_claims(document, CLAIM_SCHEMA)
        self.assertTrue(changed)
        self.assertEqual([item["section"] for item in result["claims"]], [13, 8, 4])
        self.assertEqual(document["claims"][0]["section"], "13")
        self.assertEqual(result["claims"][1]["text"], "라이브러리")

    def test_out_of_range_and_unknown_sections_are_not_silently_remapped(self):
        document = {"claims": [
            {"section": 19, "text": "X", "segment_ids": ["s000001"], "kind": "source_claim"},
            {"section": "19", "text": "Y", "segment_ids": ["s000002"], "kind": "source_claim"},
            {"section": "13. Unknown", "text": "Z", "segment_ids": ["s000003"], "kind": "source_claim"},
        ]}
        result, changed = normalize_category_claims(document, CLAIM_SCHEMA)
        self.assertFalse(changed)
        self.assertEqual(result, document)

    def test_repeated_string_sections_can_pass_adapter_after_normalization(self):
        doc = {"claims": [{"section": "13", "text": "X",
                "segment_ids": ["s000001"], "kind": "source_claim"}]}
        stdout = json.dumps({"status": "SUCCESS", "response": json.dumps(doc, ensure_ascii=False)})
        data, _ = parse_structured_response(stdout, "", CLAIM_SCHEMA)
        self.assertEqual(data["claims"][0]["section"], 13)

    def test_category_id_name_claim_rows_normalize_without_losing_evidence(self):
        source = {"claims": [
            {"category_id": 2, "category_name": "AI 모델 및 서비스",
             "claim": "원래 모델 분석 내용", "segment_ids": ["s000051", "s000052"]},
            {"category_id": 13, "category_name": "13. CLI 명령어 및 설정",
             "claim": "원래 명령 설명", "segment_ids": ["s000053"]},
        ]}
        output, changed = normalize_category_claims(source, CLAIM_SCHEMA)
        self.assertTrue(changed)
        self.assertEqual(output["claims"], [
            {"section": 2, "text": "원래 모델 분석 내용",
             "segment_ids": ["s000051", "s000052"], "kind": "source_claim"},
            {"section": 13, "text": "원래 명령 설명",
             "segment_ids": ["s000053"], "kind": "source_claim"},
        ])
        self.assertEqual(source["claims"][0]["category_id"], 2)
        self.assertNotIn("section", source["claims"][0])

    def test_four_identical_category_id_responses_produce_only_ten_claims(self):
        doc = {"claims": [
            {"category_id": 8, "category_name": "Framework / Library",
             "claim": f"내용 {i}", "segment_ids": [f"s{i:06d}"]}
            for i in range(1, 11)
        ]}
        response = "\n".join(json.dumps(doc, ensure_ascii=False) for _ in range(4))
        envelope = json.dumps({"status": "SUCCESS", "response": response})
        parsed, usage = parse_structured_response(envelope, "", CLAIM_SCHEMA)
        self.assertEqual(len(parsed["claims"]), 10)
        self.assertEqual(usage["response_json_documents"], 4)
        self.assertEqual(usage["normalized_from"], "category_fields")
        self.assertEqual(parsed["claims"][0]["segment_ids"], ["s000001"])
        self.assertEqual(parsed["claims"][-1]["text"], "내용 10")

    def test_structured_output_category_fields_are_normalized(self):
        doc = {"claims": [{
            "category_id": 12, "category_name": "Automation",
            "claim": "자동화 내용", "segment_ids": ["s000001"],
        }]}
        stdout = json.dumps({"status": "SUCCESS", "structured_output": doc}, ensure_ascii=False)
        parsed, usage = parse_structured_response(stdout, "", CLAIM_SCHEMA)
        self.assertEqual(parsed["claims"][0]["section"], 12)
        self.assertEqual(usage["normalized_from"], "category_fields")

    def test_category_id_must_agree_with_name(self):
        bad = {"claims": [{
            "category_id": 2, "category_name": "핵심 AI 주제",
            "claim": "PRIVATE_BODY_NOT_FOR_ERRORS", "segment_ids": ["s000001"]
        }]}
        with self.assertRaisesRegex(RuntimeError, "AGY_CATEGORY_NAME_MISMATCH") as caught:
            normalize_category_claims(bad, CLAIM_SCHEMA)
        self.assertNotIn("PRIVATE_BODY_NOT_FOR_ERRORS", str(caught.exception))
        self.assertNotIn("핵심 AI 주제", str(caught.exception))

    def test_category_id_out_of_range_and_missing_evidence_are_rejected(self):
        for cat in (0, 19, True, "8", -1):
            with self.subTest(cat=cat):
                data = {"claims": [{
                    "category_id": cat, "category_name": "Framework / Library",
                    "claim": "content", "segment_ids": ["s000001"],
                }]}
                with self.assertRaisesRegex(RuntimeError, "AGY_CATEGORY_ID_INVALID"):
                    normalize_category_claims(data, CLAIM_SCHEMA)
        with self.assertRaisesRegex(RuntimeError, "AGY_CATEGORY_FIELDS"):
            normalize_category_claims({"claims": [{
                "category_id": 8, "category_name": "Framework / Library",
                "claim": "content", "segment_ids": [],
            }]}, CLAIM_SCHEMA)

    def test_unknown_category_row_field_cannot_be_silently_dropped(self):
        bad = {"claims": [{
            "category_id": 8, "category_name": "Framework / Library",
            "claim": "content", "segment_ids": ["s000001"], "extra": "private data",
        }]}
        with self.assertRaisesRegex(RuntimeError, "AGY_CATEGORY_FIELDS") as caught:
            normalize_category_claims(bad, CLAIM_SCHEMA)
        self.assertNotIn("private data", str(caught.exception))

    def test_english_snake_case_18_category_schema_is_deterministic(self):
        names = [
            "core_ai_topics", "ai_models_and_services", "ai_coding_tools",
            "agent_multi_agent", "skills", "plugins_extensions_connectors",
            "mcp", "framework_library", "rag_knowledge_memory_context",
            "prompt_engineering", "api_tool_calling", "automation",
            "cli_commands_settings", "practical_workflow",
            "cost_token_performance_optimization", "comparisons",
            "failure_cases_antipatterns_cautions", "source_specific_unique_insights",
        ]
        doc = {key: [] for key in names}
        doc["core_ai_topics"] = [
            {"claim": "AI 핵심 내용", "segment_ids": ["s000002"]}
        ]
        doc["ai_models_and_services"] = [
            {"claim": "모델 비교", "segment_ids": ["s000003"]}
        ]
        doc["api_tool_calling"] = [
            {"claim": "API 도구 호출", "segment_ids": ["s000004"]}
        ]
        doc["source_specific_unique_insights"] = [
            {"claim": "고유 출처", "segment_ids": ["s000005"]}
        ]
        original = json.loads(json.dumps(doc, ensure_ascii=False))
        result, changed = normalize_category_claims(doc, CLAIM_SCHEMA)
        self.assertTrue(changed)
        self.assertEqual([x["section"] for x in result["claims"]], [1, 2, 11, 18])
        self.assertEqual(
            [x["text"] for x in result["claims"]],
            ["AI 핵심 내용", "모델 비교", "API 도구 호출", "고유 출처"]
        )
        self.assertEqual(doc, original)

    def test_four_repeated_english_category_docs_deduplicate(self):
        doc = {"core_ai_topics": [
            {"claim": "구체적인 내용", "segment_ids": ["s000001"]}],
            "ai_models_and_services": [
            {"claim": "모델별 특징", "segment_ids": ["s000002"]}],
            "api_tool_calling": [
            {"claim": "호출 예시", "segment_ids": ["s000003"]}],
            "automation": []}
        response = "\n".join(json.dumps(doc, ensure_ascii=False) for _ in range(4))
        stdout = json.dumps({"status": "SUCCESS", "response": response})
        parsed, usage = parse_structured_response(stdout, "", CLAIM_SCHEMA)
        self.assertEqual(len(parsed["claims"]), 3)
        self.assertEqual([x["section"] for x in parsed["claims"]], [1, 2, 11])
        self.assertEqual(usage["response_json_documents"], 4)
        self.assertEqual(usage["normalized_from"], "english_category_keys")

    def test_unknown_english_category_is_not_ignored_even_when_empty(self):
        response = {"core_ai_topics": [], "unknown_private_bucket": []}
        result, changed = normalize_category_claims(response, CLAIM_SCHEMA)
        self.assertFalse(changed)
        self.assertEqual(result, response)

    def test_english_category_needs_claim_and_segment_ids(self):
        for item in (
            {"claim": "missing evidence"},
            {"claim": "missing IDs", "segment_ids": []},
            {"claim": "", "segment_ids": ["s000001"]},
            {"claim": "text", "segment_ids": "s000001"},
            {"claim": "text", "segment_ids": ["s000001"], "extra": "private"},
        ):
            with self.subTest(keys=tuple(item)):
                with self.assertRaisesRegex(RuntimeError, "AGY_CATEGORY_SHAPE"):
                    normalize_category_claims({"core_ai_topics": [item]}, CLAIM_SCHEMA)

    def _fourth_chunk_shape(self):
        # Reconstructed from the user's safe local inspector: all 18 slots,
        # 8 total evidence claims, the unknown (empty) slot #15 and
        # comparison_points at #16.
        names = [
            "core_ai_topics", "ai_models_and_services", "ai_coding_tools",
            "agent_multi_agent", "skills", "plugins_extensions_connectors",
            "mcp", "framework_library", "rag_knowledge_memory_context",
            "prompt_engineering", "api_tool_calling", "automation",
            "cli_commands_and_settings", "practical_workflow",
            "cost&tokens·performance", "comparison_points",
            "failure_cases_antipatterns_warnings", "source_specific_unique_insights",
        ]
        counts = {1: 1, 2: 2, 11: 1, 14: 2, 16: 1, 18: 1}
        result = {}
        cursor = 0
        for index, name in enumerate(names, 1):
            result[name] = []
            for _ in range(counts.get(index, 0)):
                cursor += 1
                result[name].append({
                    "claim": f"영상 증거 {cursor}",
                    "segment_ids": [f"s{cursor + 150:06d}"],
                })
        return result

    def test_full_fourth_chunk_accepts_only_single_unknown_empty_slot(self):
        from ytresearch.normalization import anchored_unknown_empty_slots
        doc = self._fourth_chunk_shape()
        self.assertEqual(anchored_unknown_empty_slots(doc), (15,))
        self.assertEqual(doc["comparison_points"][0]["claim"], "영상 증거 7")
        converted, changed = normalize_category_claims(doc, CLAIM_SCHEMA)
        self.assertTrue(changed)
        self.assertEqual(len(converted["claims"]), 8)
        self.assertEqual([x["section"] for x in converted["claims"]],
                         [1, 2, 2, 11, 14, 14, 16, 18])
        self.assertEqual([x["text"] for x in converted["claims"]],
                         [f"영상 증거 {i}" for i in range(1, 9)])

    def test_repeated_fourth_chunk_deduplicates_and_records_empty_slot(self):
        doc = self._fourth_chunk_shape()
        response = "\n".join(json.dumps(doc, ensure_ascii=False) for _ in range(4))
        stdout = json.dumps({"status": "SUCCESS", "response": response}, ensure_ascii=False)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            normalized, usage = parse_structured_response(stdout, "", CLAIM_SCHEMA)
        self.assertEqual(len(normalized["claims"]), 8)
        self.assertEqual(usage["response_json_documents"], 4)
        self.assertEqual(usage["normalized_from"], "english_category_keys")
        self.assertEqual(usage["unrecognized_empty_category_slots"], [15])
        self.assertIn("슬롯 [15]", err.getvalue())

    def test_unknown_full_schema_slots_with_content_are_rejected(self):
        doc = self._fourth_chunk_shape()
        doc["cost&tokens·performance"] = [
            {"claim": "NONEMPTY MUST NOT DISAPPEAR", "segment_ids": ["s000152"]}
        ]
        normalized, changed = normalize_category_claims(doc, CLAIM_SCHEMA)
        self.assertFalse(changed)
        self.assertEqual(normalized, doc)

    def test_unknown_empty_slot_requires_all_other_ordered_anchors(self):
        from ytresearch.normalization import anchored_unknown_empty_slots
        doc = self._fourth_chunk_shape()
        doc["core_ai_topics"], doc["ai_models_and_services"] = (
            doc["ai_models_and_services"], doc["core_ai_topics"]
        )
        # Wrong values alone do not modify map order. Swap the *key* positions.
        pairs = list(doc.items())
        pairs[0], pairs[1] = pairs[1], pairs[0]
        rearranged = dict(pairs)
        self.assertEqual(anchored_unknown_empty_slots(rearranged), ())
        result, changed = normalize_category_claims(rearranged, CLAIM_SCHEMA)
        self.assertFalse(changed)
        self.assertEqual(result, rearranged)

    def test_multiple_unknown_empty_categories_are_not_accepted(self):
        doc = self._fourth_chunk_shape()
        doc.pop("skills")
        pairs = list(doc.items())
        pairs.insert(4, ("MYSTERY-5", []))
        doc = dict(pairs)
        normalized, changed = normalize_category_claims(doc, CLAIM_SCHEMA)
        self.assertFalse(changed)
        self.assertEqual(normalized, doc)

    def test_object_valued_category_fails_with_position_and_type_only(self):
        private = "PRIVATE_AI_RESPONSE_MUST_NOT_LEAK"
        document = {
            "core_ai_topics": [],
            "ai_models_and_services": {
                "items": [{"claim": private, "segment_ids": ["s000201"]}]
            },
        }
        with self.assertRaisesRegex(RuntimeError, "AGY_CATEGORY_SHAPE") as caught:
            normalize_category_claims(document, CLAIM_SCHEMA)
        self.assertIn("category_position=2", str(caught.exception))
        self.assertIn("actual_type=dict", str(caught.exception))
        self.assertNotIn(private, str(caught.exception))

    def test_do_not_change_analysis_output(self):
        source = {"sections": [{"number": 1, "markdown": "A"}]}
        self.assertEqual(normalize_category_claims(source, CLAIM_SCHEMA), (source, False))


if __name__ == "__main__":
    unittest.main()
