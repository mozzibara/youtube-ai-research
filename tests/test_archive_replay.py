"""Archive replay must be offline, exact-prompt-bound, and evidence preserving."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ytresearch.agent import Agy
from ytresearch.definitions import CLAIM_SCHEMA
from ytresearch.diagnostics import archive_agy_attempt
from ytresearch.pipeline import extract_one, analyze_one
from ytresearch.storage import atomic_write, digest, json_text, video_dir


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.config = {
            "extractor": {"model": "gemini-3.8-flash-medium", "effort": "medium"},
            "max_chars_per_chunk": 50, "max_retries": 0,
        }
        (self.root / "config" / "agents.json").write_text(
            json.dumps(self.config), encoding="utf-8"
        )
        agent_folder = self.root / ".agents" / "agents"
        agent_folder.mkdir(parents=True)
        (agent_folder / "yt-research-no-tools.md").write_text(
            "---\nname: yt-research-no-tools\ntools: []\n---\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def _save_model_output(self, prompt, unit="chunk_001", video_id="Tst00000054"):
        result = {
            "1. 핵심 AI 주제": [{"claim": "검증 가능한 주제",
                           "segment_ids": ["s000001"]}]
        }
        raw = json.dumps({
            "status": "SUCCESS",
            "response": "\n".join(
                json.dumps(result, ensure_ascii=False) for _ in range(4)
            ),
        }, ensure_ascii=False)
        return archive_agy_attempt(
            self.root, role="extractor", model="gemini-3.8-flash-medium",
            context={"video_id": video_id, "unit": unit},
            attempt=1, stdout=raw, stderr="", exit_code=0,
            error=RuntimeError("AGY_SCHEMA_MISMATCH"), prompt=prompt,
        )

    def test_archive_recovers_converted_claims_without_model_invocation(self):
        self._save_model_output("exact original prompt")
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", side_effect=AssertionError("model used")), \
             contextlib.redirect_stderr(io.StringIO()):
            result, usage = Agy(self.root).run(
                "extractor", "exact original prompt", CLAIM_SCHEMA,
                context={"video_id": "Tst00000054", "unit": "chunk_001"},
                archives_only=True,
            )
        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(result["claims"][0]["text"], "검증 가능한 주제")
        self.assertEqual(result["claims"][0]["kind"], "source_claim")
        self.assertEqual(usage["response_json_documents"], 4)
        self.assertTrue(usage["replayed_from_archive"])

    def test_offline_archived_english_categories_are_recovered_without_agy(self):
        content = {
            "core_ai_topics": [{"claim": "AI 원리", "segment_ids": ["s000153"]}],
            "ai_models_and_services": [{"claim": "모델 활용", "segment_ids": ["s000154"]}],
            "api_tool_calling": [{"claim": "API 사용", "segment_ids": ["s000155"]}],
            "automation": [],
        }
        stdout = json.dumps({
            "status": "SUCCESS",
            "response": "\n".join(json.dumps(content, ensure_ascii=False) for _ in range(4)),
        }, ensure_ascii=False)
        archive_agy_attempt(
            self.root, role="extractor", model="gemini-3.8-flash-medium",
            context={"video_id": "Tst00000054", "unit": "chunk_004"},
            attempt=1, stdout=stdout, stderr="", exit_code=0,
            error=RuntimeError("AGY_SCHEMA_MISMATCH"), prompt="exact chunk 4 prompt",
        )
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", side_effect=AssertionError("must not call model")), \
             contextlib.redirect_stderr(io.StringIO()):
            result, usage = Agy(self.root).run(
                "extractor", "exact chunk 4 prompt", CLAIM_SCHEMA,
                context={"video_id": "Tst00000054", "unit": "chunk_004"},
                archives_only=True,
            )
        self.assertEqual(len(result["claims"]), 3)
        self.assertEqual([x["section"] for x in result["claims"]], [1, 2, 11])
        self.assertEqual(result["claims"][0]["segment_ids"], ["s000153"])
        self.assertEqual(usage["normalized_from"], "english_category_keys")
        self.assertTrue(usage["replayed_from_archive"])

    def test_offline_archive_replay_with_full_18_category_response(self):
        from ytresearch.normalization import ENGLISH_CATEGORY_ALIASES
        names = [
            "core_ai_topics", "ai_models_and_services", "ai_coding_tools",
            "agent_multi_agent", "skills", "plugins_extensions_connectors",
            "mcp", "framework_library", "rag_knowledge_memory_context",
            "prompt_engineering", "api_tool_calling", "automation",
            "cli_commands_and_settings", "practical_workflow",
            "cost&tokens·performance", "comparison_points",
            "failure_cases_antipatterns_warnings", "source_specific_unique_insights",
        ]
        document = {name: [] for name in names}
        document["core_ai_topics"] = [
            {"claim": "첫 내용", "segment_ids": ["s000154"]}]
        document["comparison_points"] = [
            {"claim": "비교 내용", "segment_ids": ["s000156"]}]
        prompt = "exact archived fourth chunk prompt"
        archive_agy_attempt(
            self.root, role="extractor", model="gemini-3.8-flash-medium",
            context={"video_id": "Tst00000054", "unit": "chunk_004"},
            attempt=1, stdout=json.dumps({
                "status": "SUCCESS",
                "response": "\n".join(
                    json.dumps(document, ensure_ascii=False) for _ in range(4)
                ),
            }, ensure_ascii=False),
            stderr="", exit_code=0, prompt=prompt,
            error=RuntimeError("AGY_SCHEMA_MISMATCH"),
        )
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", side_effect=AssertionError("network forbidden")), \
             contextlib.redirect_stderr(io.StringIO()):
            result, usage = Agy(self.root).run(
                "extractor", prompt, CLAIM_SCHEMA, archives_only=True,
                context={"video_id": "Tst00000054", "unit": "chunk_004"},
            )
        self.assertEqual([item["section"] for item in result["claims"]], [1, 16])
        self.assertEqual(usage["unrecognized_empty_category_slots"], [15])
        self.assertTrue(usage["replayed_from_archive"])

    def test_mixed_archive_requires_explicit_quarantine_and_preserves_raw(self):
        names = [
            "core_ai_topics", "ai_models_and_services", "ai_coding_tools",
            "agent_multi_agent", "skills", "plugins_extensions_connectors",
            "mcp", "framework_library", "rag_knowledge_memory_context",
            "prompt_engineering", "api_tool_calling", "automation",
            "cli_commands_and_settings", "practical_workflow",
            "cost&tokens·performance", "comparisons",
            "failure_cases_antipatterns_warnings", "source_specific_unique_insights",
        ]
        doc = {n: [] for n in names}
        doc["core_ai_topics"] = [
            {"claim": "AI 주제", "segment_ids": ["s000202"]},
        ]
        doc["rag_knowledge_memory_context"] = [
            {"claim": "RAG 근거", "segment_ids": ["s000208"]},
        ]
        synthetic_strings = {name: "UNSTRUCTURED_TEXT_VALUE" for name in names}
        docs = [
            doc,
            {"status": "completed", "message": "PRIVATE REVIEW NOTE"},
            synthetic_strings,
            synthetic_strings,
        ]
        prompt = "exact archived fifth chunk prompt"
        stdout = json.dumps({
            "status": "SUCCESS",
            "response": "\n".join(json.dumps(item, ensure_ascii=False) for item in docs),
        }, ensure_ascii=False)
        path = archive_agy_attempt(
            self.root, role="extractor", model="gemini-3.8-flash-medium",
            context={"video_id": "Tst00000054", "unit": "chunk_005"},
            attempt=1, stdout=stdout, stderr="", exit_code=0,
            error=RuntimeError("AGY_CATEGORY_SHAPE"), prompt=prompt,
        )
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", side_effect=AssertionError("model must not run")), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                Agy(self.root).run(
                    "extractor", prompt, CLAIM_SCHEMA, archives_only=True,
                    context={"video_id": "Tst00000054", "unit": "chunk_005"},
                )
            claims, usage = Agy(self.root).run(
                "extractor", prompt, CLAIM_SCHEMA, archives_only=True,
                quarantine_mixed=True,
                context={"video_id": "Tst00000054", "unit": "chunk_005"},
            )
        self.assertEqual([c["section"] for c in claims["claims"]], [1, 9])
        self.assertTrue(usage["review_required"])
        self.assertTrue(usage["replayed_from_archive"])
        self.assertEqual(usage["quarantined_document_indices"], [2, 3, 4])
        self.assertEqual((path / "stdout.raw.txt").read_text(encoding="utf-8"), stdout)
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"):
            with self.assertRaisesRegex(RuntimeError, "AGY_QUARANTINE_OFFLINE_ONLY"):
                Agy(self.root).run(
                    "extractor", prompt, CLAIM_SCHEMA, quarantine_mixed=True,
                )

    def test_quarantined_chunk_is_cached_and_blocks_unreviewed_analysis(self):
        vid = "Tst00000054"
        path = video_dir(self.root, vid)
        original = json_text({
            "video_id": vid,
            "segments": [{
                "id": "s000001", "start": 0.0, "duration": 1.0,
                "text": "영상 원본의 근거 문장",
            }],
        })
        atomic_write(path / "transcript.json", original)
        atomic_write(path / "transcript.sha256", digest(original) + "\n")
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"):
            runner = Agy(self.root)
        with patch.object(Agy, "run", side_effect=RuntimeError("capture prompt")) as spy, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "capture prompt"):
                extract_one(self.root, vid, runner, archives_only=True, quarantine_mixed=True)
        prompt = spy.call_args.args[1]
        names = [
            "core_ai_topics", "ai_models_and_services", "ai_coding_tools",
            "agent_multi_agent", "skills", "plugins_extensions_connectors",
            "mcp", "framework_library", "rag_knowledge_memory_context",
            "prompt_engineering", "api_tool_calling", "automation",
            "cli_commands_and_settings", "practical_workflow",
            "cost&tokens·performance", "comparisons",
            "failure_cases_antipatterns_warnings", "source_specific_unique_insights",
        ]
        first = {name: [] for name in names}
        first["core_ai_topics"] = [
            {"claim": "원문에서 검증 가능한 주장", "segment_ids": ["s000001"]}
        ]
        labels = {name: "No structured claims." for name in names}
        docs = [
            first, {"status": "completed", "message": "Extra unstructured details"},
            labels, labels,
        ]
        archive_agy_attempt(
            self.root, role="extractor", model="gemini-3.8-flash-medium",
            context={"video_id": vid, "unit": "chunk_001"}, attempt=1,
            stdout=json.dumps({
                "status": "SUCCESS",
                "response": "\n".join(
                    json.dumps(doc, ensure_ascii=False) for doc in docs
                ),
            }, ensure_ascii=False),
            stderr="", exit_code=0, prompt=prompt,
            error=RuntimeError("AGY_CATEGORY_SHAPE"),
        )
        with patch.object(Agy, "_invoke", side_effect=AssertionError("no network")), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            count, chunks = extract_one(
                self.root, vid, runner, archives_only=True, quarantine_mixed=True
            )
        self.assertEqual((count, chunks), (1, 1))
        saved = json.loads((path / "extraction" / "chunk_001.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["usage"]["review_required"])
        combined = json.loads((path / "extraction.json").read_text(encoding="utf-8"))
        self.assertTrue(combined["review_required"])
        self.assertEqual(combined["quarantined_chunks"][0]["chunk"], 1)
        self.assertEqual(len(combined["claims"]), 1)
        with self.assertRaisesRegex(RuntimeError, "AGY_REVIEW_REQUIRED"):
            analyze_one(self.root, vid, runner)

    def test_prompt_mismatch_refuses_to_call_model(self):
        self._save_model_output("original prompt")
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", side_effect=AssertionError("model used")):
            with self.assertRaisesRegex(RuntimeError, "AGY_ARCHIVE_NOT_FOUND"):
                Agy(self.root).run(
                    "extractor", "modified prompt", CLAIM_SCHEMA,
                    context={"video_id": "Tst00000054", "unit": "chunk_001"},
                    archives_only=True,
                )

    def test_tampered_archive_refused(self):
        folder = self._save_model_output("original prompt")
        (folder / "stdout.raw.txt").write_text("{}", encoding="utf-8")
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", side_effect=AssertionError("model used")):
            with self.assertRaisesRegex(RuntimeError, "AGY_ARCHIVE_TAMPERED"):
                Agy(self.root).run(
                    "extractor", "original prompt", CLAIM_SCHEMA,
                    context={"video_id": "Tst00000054", "unit": "chunk_001"},
                    archives_only=True,
                )

    def test_pipeline_saves_first_recovered_chunk_then_stops_offline(self):
        vid = "Tst00000054"
        path = video_dir(self.root, vid)
        source = {
            "video_id": vid,
            "segments": [
                {"id": f"s{i:06d}", "start": float(i), "duration": 1.0,
                 "text": "짧은 실제 원문 한 문장"} for i in range(1, 3)
            ],
        }
        raw = json_text(source)
        atomic_write(path / "transcript.json", raw)
        atomic_write(path / "transcript.sha256", digest(raw) + "\n")
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"):
            runner = Agy(self.root)
        with patch.object(Agy, "run", side_effect=RuntimeError("capture prompt")) as spy, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "capture prompt"):
                extract_one(self.root, vid, runner, archives_only=True)
        actual_prompt = spy.call_args.args[1]
        self._save_model_output(actual_prompt)
        with patch.object(Agy, "_invoke", side_effect=AssertionError("model used")), \
             contextlib.redirect_stdout(io.StringIO()) as console, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "AGY_ARCHIVE_NOT_FOUND"):
                extract_one(self.root, vid, runner, archives_only=True)
        self.assertIn("50%", console.getvalue())
        first = path / "extraction" / "chunk_001.json"
        self.assertTrue(first.is_file())
        self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["claims"][0]["text"],
                         "검증 가능한 주제")
        self.assertFalse((path / "extraction" / "chunk_002.json").exists())


if __name__ == "__main__":
    unittest.main()
