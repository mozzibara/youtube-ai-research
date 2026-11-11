"""Regression tests for private raw model-output archiving and safe CLI summaries."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ytresearch.agent import Agy
from ytresearch.diagnostics import archive_agy_attempt, list_diagnostic_summaries, inspect_saved_response_structure, inspect_saved_claim_sections, inspect_saved_document_summary, inspect_video_access_archive
from ytresearch import __main__ as cli


class RawDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "agents.json").write_text(json.dumps({
            "extractor": {"model": "gemini-3.8-flash-medium", "effort": "medium"},
            "max_retries": 0, "archive_agy_responses": True,
        }), encoding="utf-8")
        agents = self.root / ".agents" / "agents"
        agents.mkdir(parents=True)
        (agents / "yt-research-no-tools.md").write_text("---\nname: yt-research-no-tools\ntools: []\n---\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_video_access_diagnostics_do_not_leak_raw_url_or_model_text(self):
        vid = "Tst00000075"
        token = "PRIVATE_AUTH_TOKEN_2026"
        events = [
            {
                "event": "init",
                "init": {
                    "tools": ["read_url_content", "invoke_subagent", "run_command"],
                    "permission_mode": "request-review",
                    "agent": "yt-research-video",
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool", "state": "DONE",
                    "tool_name": "read_url_content",
                    "tool_info": {
                        "parameters": {
                            "url": "https://www.youtube.com/watch?v=" + vid,
                            "credential": token,
                        },
                        "error": {"message": "permission denied " + token},
                    },
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "SOURCE_ACCESS_FAILED\n" + token,
                },
            },
        ]
        raw = "\n".join(json.dumps(item) for item in events)
        archive_agy_attempt(
            self.root, role="video_source", model="model", attempt=1,
            context={"video_id": vid, "unit": "full"},
            stdout=raw, stderr="approval required " + token, exit_code=0,
            error=RuntimeError("AGY_VIDEO_SOURCE_NOT_OBSERVED"),
        )
        summary = inspect_video_access_archive(self.root, vid)
        self.assertTrue(summary["found"])
        self.assertTrue(summary["init_seen"])
        self.assertEqual(summary["agent_selection"], "matched")
        self.assertEqual(summary["permission_mode"], "request-review")
        self.assertEqual(summary["step_type_counts"], {"tool": 1})
        self.assertEqual(summary["available_web_tools"], ["read_url_content"])
        self.assertEqual(summary["tool_done_count"], 1)
        self.assertEqual(summary["tool_target_match_count"], 1)
        self.assertEqual(summary["tool_error_count"], 1)
        self.assertEqual(summary["successful_target_web_steps"], 0)
        self.assertEqual(summary["access_level"], "not_observed")
        self.assertEqual(summary["next_action"], "CHECK_SCOPED_READ_URL_PERMISSIONS")
        self.assertTrue(summary["source_access_failed_response"])
        self.assertTrue(summary["permission_hint"])
        self.assertNotIn(token, json.dumps(summary))
        self.assertNotIn("https://www.youtube.com/watch", json.dumps(summary))
        from ytresearch import __main__ as cli
        args = [
            "ytresearch", "--root", str(self.root), "diagnostics",
            "--video-id", vid, "--inspect-video-access",
        ]
        with patch.object(sys, "argv", args), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 0)
        self.assertIn("CHECK_SCOPED_READ_URL_PERMISSIONS", output.getvalue())
        self.assertNotIn(token, output.getvalue())

    def test_video_access_diagnosis_distinguishes_no_tool_and_subagent_only(self):
        vid = "Tst00000075"
        events = [
            {
                "event": "init",
                "init": {"tools": ["read_url_content", "invoke_subagent"]},
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool", "state": "DONE",
                    "tool_name": "invoke_subagent",
                    "tool_info": {
                        "parameters": {
                            "task": "research https://youtube.com/watch?v=" + vid
                        }
                    },
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS", "response": "00:00 guessed page summary"
                },
            },
        ]
        archive_agy_attempt(
            self.root, role="video_source", model="model", attempt=1,
            context={"video_id": vid, "unit": "full"},
            stdout="\n".join(json.dumps(item) for item in events),
            stderr="", exit_code=0,
        )
        report = inspect_video_access_archive(self.root, vid)
        self.assertEqual(report["tool_target_match_count"], 1)
        self.assertEqual(report["tool_done_count"], 1)
        self.assertEqual(report["successful_target_web_steps"], 0)
        self.assertEqual(report["access_level"], "not_observed")
        self.assertEqual(report["next_action"], "WEB_TOOL_NOT_SUCCESSFULLY_OBSERVED")

    def test_video_access_diagnosis_reports_missing_archive_without_network(self):
        self.assertEqual(
            inspect_video_access_archive(self.root, "Tst00000075"),
            {"found": False, "reason": "NO_ARCHIVED_VIDEO_ATTEMPT"},
        )

    def test_raw_capture_exact_and_summary_private(self):
        secret_url = "https://accounts.google.com/session-sensitive-token"
        stdout = '{"event":"result","result":{"status":"ERROR","error":"' + secret_url + '"}}\r\n'
        stderr = "인증 실패 " + secret_url + "\r\n"
        with patch("ytresearch.diagnostics.uuid4") as uuid:
            uuid.return_value.hex = "abc123def456"
            directory = archive_agy_attempt(
                self.root, role="extractor", model="gemini-3.8-flash-medium",
                attempt=1, context={"video_id": "Tst00000054", "unit": "chunk_001"},
                stdout=stdout, stderr=stderr, exit_code=1,
                error=RuntimeError("AGY_EXIT_NONZERO"), prompt="sensitive prompt",
            )
        self.assertTrue(directory.is_relative_to(self.root / "data" / "diagnostics"))
        self.assertEqual((directory / "stdout.raw.txt").read_bytes(), stdout.encode("utf-8"))
        self.assertEqual((directory / "stderr.raw.txt").read_bytes(), stderr.encode("utf-8"))
        metadata_text = (directory / "summary.json").read_text(encoding="utf-8")
        self.assertNotIn(secret_url, metadata_text)
        self.assertNotIn("sensitive prompt", metadata_text)
        summary = json.loads(metadata_text)
        self.assertEqual(summary["model"], "gemini-3.8-flash-medium")
        self.assertEqual(summary["error_code"], "AGY_EXIT_NONZERO")
        self.assertEqual(summary["video_id"], "Tst00000054")

    def test_archive_creates_unique_folders_and_blocks_path_traversal(self):
        folders = [
            archive_agy_attempt(
                self.root, role="extractor", model="model", attempt=1,
                context={"video_id": "..", "unit": "../../passwords"},
                stdout="{}", stderr="", exit_code=0,
            )
            for _ in range(2)
        ]
        self.assertNotEqual(folders[0], folders[1])
        for folder in folders:
            self.assertTrue(folder.is_relative_to(self.root / "data" / "diagnostics"))
            self.assertIn("unknown-video", str(folder))

    def test_cli_failure_is_archived_before_raising(self):
        response = json.dumps({"status": "SUCCESS", "response": "not-json"})
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout=response, stderr=""
             )), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "AGY_UNSTRUCTURED_RESPONSE"):
                Agy(self.root).run(
                    "extractor", "some prompt",
                    {"type": "object", "properties": {"claims": {"type": "array"}}, "required": ["claims"]},
                    context={"video_id": "Tst00000054", "unit": "chunk_001"},
                )
        entries = list_diagnostic_summaries(self.root, video_id="Tst00000054")
        self.assertEqual(len(entries), 1)
        path, summary = entries[0]
        self.assertEqual(summary["outcome"], "adapter_failed")
        self.assertEqual(summary["unit"], "chunk_001")
        self.assertEqual((path.parent / "stdout.raw.txt").read_text(encoding="utf-8"), response)

    def test_successful_adapter_call_is_also_archived(self):
        # The pipeline might still reject references after the adapter parsed.
        response = json.dumps({"status": "SUCCESS", "structured_output": {"claims": []}})
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout=response, stderr=""
             )), contextlib.redirect_stderr(io.StringIO()):
            answer, _ = Agy(self.root).run(
                "extractor", "some prompt",
                {"type": "object", "properties": {"claims": {"type": "array"}}, "required": ["claims"]},
                context={"video_id": "Tst00000054", "unit": "chunk_001"},
            )
        self.assertEqual(answer, {"claims": []})
        items = list_diagnostic_summaries(self.root)
        self.assertEqual(items[0][1]["outcome"], "adapter_parsed")

    def test_local_key_inspector_reports_korean_schema_without_values(self):
        secret = "SENSITIVE_ANALYSIS_BODY_NEVER_DISPLAY"
        object_value = {"핵심 AI 주제": [{"설명": secret, "근거": ["s000001"]}],
                        "모델 비교": [{"내용": secret}]}
        response = "\n".join(json.dumps(object_value, ensure_ascii=False) for _ in range(4))
        ndjson = "\n".join([
            json.dumps({"event": "init", "init": {}}),
            json.dumps({"event": "result", "result": {
                "status": "SUCCESS", "response": response
            }}, ensure_ascii=False),
        ])
        directory = archive_agy_attempt(
            self.root, role="extractor", model="model", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_001"},
            stdout=ndjson, stderr="", exit_code=0,
        )
        structure = inspect_saved_response_structure(directory)
        formatted = json.dumps(structure, ensure_ascii=False)
        self.assertEqual(structure["documents_decoded"], 4)
        self.assertTrue(structure["documents_identical"])
        self.assertIn("핵심 AI 주제", formatted)
        self.assertIn("모델 비교", formatted)
        self.assertNotIn(secret, formatted)
        self.assertEqual(structure["trailing_unparsed_chars"], 0)

        # The CLI prints keys only; the export remains metadata-only.
        with patch.object(sys, "argv", [
            "ytresearch", "--root", str(self.root), "diagnostics",
            "--video-id", "Tst00000054", "--inspect-keys",
        ]), contextlib.redirect_stdout(io.StringIO()) as console:
            rc = cli.main()
        self.assertEqual(rc, 0)
        self.assertIn("핵심 AI 주제", console.getvalue())
        self.assertNotIn(secret, console.getvalue())

    def test_local_inspector_redacts_suspicious_keys_and_keeps_tail_private(self):
        secret_key = "https://accounts.google.com/secret"
        output = '{"claims":[]}\nNOT_JSON_AUTHENTICATION_DETAIL'
        directory = archive_agy_attempt(
            self.root, role="extractor", model="model", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_001"},
            stdout=json.dumps({"status": "SUCCESS", "response":
                json.dumps({secret_key: ["sensitive"]}) + "\nPRIVATE_TAIL"}),
            stderr="", exit_code=0,
        )
        structure = inspect_saved_response_structure(directory)
        formatted = json.dumps(structure, ensure_ascii=False)
        self.assertIn("<redacted-key>", formatted)
        self.assertNotIn(secret_key, formatted)
        self.assertNotIn("PRIVATE_TAIL", formatted)
        self.assertGreater(structure["trailing_unparsed_chars"], 0)

    def test_inspect_sections_detects_invalid_values_without_content(self):
        secret = "CONFIDENTIAL_SOURCE_LINE_SHOULD_NOT_LEAK"
        claims = [
            {"section": 1, "text": secret},
            {"section": "13", "text": secret},
            {"section": 19, "text": secret},
            {"section": "19", "text": secret},
            {"section": "5. Skills", "text": secret},
            {"section": "PRIVATE LEAK " + secret, "text": secret},
            {"section": True, "text": secret},
        ]
        stdout = json.dumps({
            "event": "result",
            "result": {"status": "SUCCESS", "response": json.dumps({"claims": claims})},
        })
        folder = archive_agy_attempt(
            self.root, role="extractor", model="gemini", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_002"},
            stdout=stdout, stderr="", exit_code=0,
        )
        diagnostics = inspect_saved_claim_sections(folder)
        doc = diagnostics["documents"][0]
        self.assertEqual(doc["claim_count"], 7)
        self.assertEqual(doc["invalid_total"], 4)
        self.assertEqual(doc["section_histogram"]["int:19"], 1)
        self.assertEqual(doc["section_histogram"]["numeric_string:19"], 1)
        rendered = json.dumps(diagnostics, ensure_ascii=False)
        self.assertNotIn(secret, rendered)
        self.assertIn("canonical_heading:5", rendered)
        with patch.object(sys, "argv", [
            "ytresearch", "--root", str(self.root), "diagnostics",
            "--video-id", "Tst00000054", "--inspect-sections", "--limit", "1",
        ]), contextlib.redirect_stdout(io.StringIO()) as captured:
            rc = cli.main()
        self.assertEqual(rc, 0)
        self.assertIn("int:19", captured.getvalue())
        self.assertNotIn(secret, captured.getvalue())

    def test_inspector_distinguishes_missing_and_null_and_hides_values(self):
        secret = "PRIVATE_TRANSCRIPT_OUTPUT_SHOULD_NOT_APPEAR"
        claims = [
            {"claim": secret, "segment_ids": ["s000001"], "category": "2. AI 모델 및 서비스"},
            {"section": None, "claim": secret, "segment_ids": ["s000002"]},
            {"section": 1, "text": secret, "segment_ids": ["s000003"], "kind": "source_claim"},
            {"person@example.com": secret, "segment_ids": ["s000004"]},
        ]
        folder = archive_agy_attempt(
            self.root, role="extractor", model="gemini", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_002"},
            stdout=json.dumps({
                "status": "SUCCESS",
                "response": json.dumps({"claims": claims}, ensure_ascii=False),
            }, ensure_ascii=False),
            stderr="", exit_code=0,
        )
        result = inspect_saved_claim_sections(folder)["documents"][0]
        self.assertEqual(result["section_missing_count"], 2)
        self.assertEqual(result["section_null_count"], 1)
        self.assertEqual(result["alternate_section_field_types"]["category"], "str")
        self.assertEqual(sum(x["count"] for x in result["claim_field_patterns"]), 4)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("claim", serialized)
        self.assertIn("category", serialized)
        self.assertIn("<redacted-key>", serialized)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("person@example.com", serialized)
        self.assertNotIn("2. AI 모델 및 서비스", serialized)

    def test_category_id_diagnostic_reports_canonical_match_counts_without_values(self):
        rows = [
            {"category_id": 2, "category_name": "AI 모델 및 서비스",
             "claim": "SECRET CLAIM BODY", "segment_ids": ["s000001"]},
            {"category_id": 13, "category_name": "13. CLI 명령어 및 설정",
             "claim": "SECRET CLAIM BODY", "segment_ids": ["s000002"]},
            {"category_id": 17, "category_name": "wrong SECRET CATEGORY",
             "claim": "SECRET CLAIM BODY", "segment_ids": ["s000003"]},
        ]
        folder = archive_agy_attempt(
            self.root, role="extractor", model="gemini", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_002"},
            stdout=json.dumps({"status": "SUCCESS",
                               "response": json.dumps({"claims": rows}, ensure_ascii=False)}),
            stderr="", exit_code=0,
        )
        result = inspect_saved_claim_sections(folder)["documents"][0]
        self.assertEqual(result["category_ids"], {"2": 1, "13": 1, "17": 1})
        self.assertEqual(result["category_name_matches"], 2)
        self.assertEqual(result["category_name_mismatches"], 1)
        self.assertEqual(result["section_missing_count"], 3)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("SECRET", serialized)

    def test_inspect_object_valued_category_exposes_structure_not_text(self):
        secret = "PRIVATE_ANALYSIS_WITH_API_TOKEN_SHOULD_NOT_LOG"
        document = {
            "core_ai_topics": {
                "items": [{"claim": secret, "segment_ids": ["s000201"]}],
                "commentary": secret,
                "token": secret,
                "metadata": {"source": secret},
            },
            "ai_models_and_services": [{"claim": secret, "segment_ids": ["s000202"]}],
        }
        stdout = json.dumps({
            "event": "result",
            "result": {"status": "SUCCESS", "response": json.dumps(document)},
        })
        folder = archive_agy_attempt(
            self.root, role="extractor", model="gemini", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_005"},
            stdout=stdout, stderr="", exit_code=0,
        )
        observed = inspect_saved_response_structure(folder)
        fields = observed["documents"][0]["fields"]
        self.assertEqual(fields[0]["key"], "core_ai_topics")
        self.assertEqual(fields[0]["type"], "dict")
        self.assertEqual(fields[0]["field_count"], 4)
        self.assertIn({"name": "items", "type": "list", "count": 1},
                      fields[0]["fields"])
        self.assertIn({"name": "<redacted-key>", "type": "str", "count": None},
                      fields[0]["fields"])
        self.assertEqual(fields[1]["item_type"], "dict")
        self.assertEqual(fields[1]["item_field_types"], [
            {"name": "claim", "type": "str"},
            {"name": "segment_ids", "type": "list"},
        ])
        rendered = json.dumps(observed, ensure_ascii=False)
        self.assertNotIn(secret, rendered)

    def test_mixed_four_documents_safe_string_probe_preserves_private_data(self):
        secret = "PRIVATE_UNSHAREABLE_BODY_AND_AUTH_TOKEN"
        evidence = {
            "core_ai_topics": [{"claim": secret, "segment_ids": ["s000200"]}],
            "ai_models_and_services": [],
        }
        status_document = {"status": "complete", "message": secret}
        string_categories = {
            "core_ai_topics": json.dumps([
                {"claim": secret, "segment_ids": ["s000200"]}
            ]),
            "ai_models_and_services": "[]",
            "mcp": "Not applicable; " + secret,
            "automation": " ",
        }
        response = "\n".join(json.dumps(doc) for doc in (
            evidence, status_document, string_categories, string_categories
        ))
        folder = archive_agy_attempt(
            self.root, role="extractor", model="gemini", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_005"},
            stdout=json.dumps({"status": "SUCCESS", "response": response}),
            stderr="", exit_code=0,
        )
        observed = inspect_saved_response_structure(folder)
        self.assertEqual(observed["documents_decoded"], 4)
        self.assertFalse(observed["documents_identical"])
        self.assertEqual(observed["documents"][2]["content_fingerprint"],
                         observed["documents"][3]["content_fingerprint"])
        entries = observed["documents"][2]["fields"]
        self.assertEqual(entries[0]["string_kind"], "json_list")
        self.assertEqual(entries[0]["decoded_items"], 1)
        self.assertEqual(entries[0]["decoded_first_item_keys"], ["claim", "segment_ids"])
        self.assertEqual(entries[1]["string_kind"], "json_list")
        self.assertEqual(entries[1]["decoded_items"], 0)
        self.assertEqual(entries[2]["string_kind"], "not_json")
        self.assertEqual(entries[3]["string_kind"], "blank")
        private_view = json.dumps(observed, ensure_ascii=False)
        self.assertNotIn(secret, private_view)
        self.assertNotIn("Not applicable", private_view)

    def test_compact_mixed_document_summary_no_model_content(self):
        secret = "PRIVATE_SOURCE_AND_AUTH_TOKEN"
        doc = {
            "core_ai_topics": [{"claim": secret, "segment_ids": ["s000195"]}],
            "ai_models_and_services": [],
            "prompt_engineering": [
                {"claim": secret, "segment_ids": ["s000199"]},
            ],
        }
        metadata = {"status": "other", "message": secret}
        string_doc = {
            "core_ai_topics": json.dumps([
                {"claim": secret, "segment_ids": ["s000195"]}
            ]),
            "ai_models_and_services": "[]",
            "mcp": "No evidence: " + secret,
        }
        stdout = json.dumps({
            "status": "SUCCESS",
            "response": "\n".join(json.dumps(obj) for obj in
                (doc, metadata, string_doc, string_doc)),
        })
        folder = archive_agy_attempt(
            self.root, role="extractor", model="gemini", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_005"},
            stdout=stdout, stderr="", exit_code=0,
        )
        summary = inspect_saved_document_summary(folder)
        self.assertEqual(summary["documents_decoded"], 4)
        self.assertFalse(summary["documents_identical"])
        self.assertEqual(summary["documents"][0]["list_evidence_records"], 2)
        self.assertEqual(summary["documents"][1]["field_types"], {"str": 2})
        self.assertEqual(summary["documents"][2]["embedded_json_evidence_records"], 1)
        self.assertEqual(summary["documents"][2]["string_kinds"]["json_list"], 2)
        self.assertEqual(summary["documents"][2]["string_kinds"]["not_json"], 1)
        self.assertEqual(summary["documents"][2]["content_fingerprint"],
                         summary["documents"][3]["content_fingerprint"])
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("s000195", encoded)
        with patch.object(sys, "argv", [
            "ytresearch", "--root", str(self.root), "diagnostics",
            "--video-id", "Tst00000054", "--inspect-summary", "--limit", "1",
        ]), contextlib.redirect_stdout(io.StringIO()) as console:
            result = cli.main()
        self.assertEqual(result, 0)
        self.assertNotIn(secret, console.getvalue())
        self.assertIn("embedded_json_evidence_records", console.getvalue())

    def test_diagnostics_cli_exports_summary_only(self):
        secret = "PRIVATE_CONTENT_NOT_FOR_GITHUB"
        archive_agy_attempt(
            self.root, role="extractor", model="model", attempt=1,
            context={"video_id": "Tst00000054", "unit": "chunk_001"},
            stdout=json.dumps({"status": "SUCCESS", "response": secret}),
            stderr=secret, exit_code=0, error=RuntimeError("AGY_UNSTRUCTURED_RESPONSE"),
        )
        output = self.root / "review" / "diagnostics.json"
        with patch.object(sys, "argv", [
            "ytresearch", "--root", str(self.root), "diagnostics",
            "--video-id", "Tst00000054", "--export-safe", str(output),
        ]), contextlib.redirect_stdout(io.StringIO()) as console:
            rc = cli.main()
        self.assertEqual(rc, 0)
        self.assertTrue(output.is_file())
        self.assertNotIn(secret, output.read_text(encoding="utf-8"))
        self.assertNotIn(secret, console.getvalue())
        self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 1)


if __name__ == "__main__":
    unittest.main()
