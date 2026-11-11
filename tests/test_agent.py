"""Antigravity CLI envelope parsing regression tests (no API/account required)."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ytresearch.agent import Agy, parse_structured_response
from ytresearch.progress import report


class AgyEnvelopeTests(unittest.TestCase):
    def test_structured_output_success(self):
        raw = json.dumps({
            "status": "SUCCESS",
            "response": json.dumps({"claims": []}),
            "structured_output": {"claims": []},
            "usage": {"output_tokens": 42},
        })
        data, usage = parse_structured_response(raw, "")
        self.assertEqual(data, {"claims": []})
        self.assertEqual(usage["output_tokens"], 42)

    def test_json_response_fallback(self):
        raw = json.dumps({
            "status": "SUCCESS",
            "response": json.dumps({"claims": []}),
            "usage": {},
        })
        data, _ = parse_structured_response(raw, "")
        self.assertEqual(data, {"claims": []})

    def test_success_without_output_is_explicit_error(self):
        raw = json.dumps({
            "status": "SUCCESS", "response": "", "num_turns": 1,
            "usage": {"output_tokens": 0},
        })
        with self.assertRaisesRegex(RuntimeError, "AGY_EMPTY_STRUCTURED_RESULT"):
            parse_structured_response(raw, "jetski: no output produced - command auto-denied")

    def test_empty_stdout_is_explicit_error(self):
        with self.assertRaisesRegex(RuntimeError, "AGY_EMPTY_STDOUT"):
            parse_structured_response("", "")

    def test_malformed_envelope_is_explicit_error(self):
        with self.assertRaisesRegex(RuntimeError, "AGY_INVALID_ENVELOPE_JSON"):
            parse_structured_response("{", "")

    def test_nonjson_response_is_explicit_error(self):
        raw = json.dumps({"status": "SUCCESS", "response": "not JSON"})
        with self.assertRaisesRegex(RuntimeError, "AGY_UNSTRUCTURED_RESPONSE"):
            parse_structured_response(raw, "")

    def test_schema_rejects_missing_claims_root(self):
        schema = {
            "type": "object", "properties": {"claims": {"type": "array"}},
            "required": ["claims"], "additionalProperties": False,
        }
        raw = json.dumps({"status": "SUCCESS", "response": '{"reasoning":"done"}', "num_turns": 2})
        with self.assertRaisesRegex(RuntimeError, "AGY_SCHEMA_MISMATCH") as context:
            parse_structured_response(raw, "", schema)
        self.assertIn("missing=['claims']", str(context.exception))
        self.assertNotIn("done", str(context.exception))

    def test_schema_rejects_wrong_claim_type(self):
        schema = {
            "type": "object", "properties": {"claims": {"type": "array"}},
            "required": ["claims"], "additionalProperties": False,
        }
        raw = json.dumps({"status": "SUCCESS", "response": '{"claims":"not a list"}'})
        with self.assertRaisesRegex(RuntimeError, "got=str"):
            parse_structured_response(raw, "", schema)

    def test_schema_rejects_invalid_second_document(self):
        schema = {
            "type": "object", "properties": {"claims": {"type": "array"}},
            "required": ["claims"], "additionalProperties": False,
        }
        raw = json.dumps({"status": "SUCCESS", "response": '{"claims":[]}\n{"status":"irrelevant"}'})
        with self.assertRaisesRegex(RuntimeError, "document=2/2"):
            parse_structured_response(raw, "", schema)

    def test_schema_error_reports_all_document_shapes_without_content(self):
        schema = {
            "type": "object", "properties": {"claims": {"type": "array"}},
            "required": ["claims"], "additionalProperties": False,
        }
        secret = "private-transcript-DO-NOT-PRINT"
        documents = [
            {"thinking": secret},
            {"claims": [{"section": 1, "text": "fixture", "segment_ids": ["s000001"], "kind": "source_claim"}]},
            {"diagnostic_note": secret, "claims": []},
            {"claims": []},
        ]
        raw = json.dumps({
            "status": "SUCCESS",
            "response": "\n".join(json.dumps(x) for x in documents),
            "num_turns": 4,
        })
        with self.assertRaisesRegex(RuntimeError, "AGY_RESPONSE_SHAPES") as caught:
            parse_structured_response(raw, "", schema)
        message = str(caught.exception)
        self.assertIn("invalid_documents=[1, 3]", message)
        self.assertIn("thinking:string(", message)
        self.assertIn("claims:list(1)", message)
        self.assertIn("diagnostic_note:string(", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("fixture", message)

    def test_schema_error_redacts_non_identifier_json_keys(self):
        schema = {
            "type": "object", "properties": {"claims": {"type": "array"}},
            "required": ["claims"], "additionalProperties": False,
        }
        raw = json.dumps({
            "status": "SUCCESS",
            "response": json.dumps({"contact@example.com": "secret-value"}),
        })
        with self.assertRaisesRegex(RuntimeError, "<redacted-key>") as caught:
            parse_structured_response(raw, "", schema)
        self.assertNotIn("contact@example.com", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))

    def test_schema_rejects_wrong_structured_output(self):
        schema = {
            "type": "object", "properties": {"claims": {"type": "array"}},
            "required": ["claims"], "additionalProperties": False,
        }
        raw = json.dumps({"status": "SUCCESS", "structured_output": {"claims": {}}})
        with self.assertRaisesRegex(RuntimeError, "AGY_SCHEMA_MISMATCH"):
            parse_structured_response(raw, "", schema)

    def test_auth_url_not_leaked(self):
        secret = "https://accounts.google.com/secret-session-token"
        raw = json.dumps({"status": "ERROR", "response": "", "error": secret})
        with self.assertRaises(RuntimeError) as context:
            parse_structured_response(raw, "account verification")
        self.assertNotIn(secret, str(context.exception))


class MixedQuarantineTests(unittest.TestCase):
    """Real five-chunk shape, requiring explicit offline-only quarantine opt-in."""
    def setUp(self):
        from ytresearch.definitions import CLAIM_SCHEMA
        self.schema = CLAIM_SCHEMA
        self.headings = [
            "core_ai_topics", "ai_models_and_services", "ai_coding_tools",
            "agent_multi_agent", "skills", "plugins_extensions_connectors",
            "mcp", "framework_library", "rag_knowledge_memory_context",
            "prompt_engineering", "api_tool_calling", "automation",
            "cli_commands_and_settings", "practical_workflow",
            "cost&tokens·performance", "comparisons",
            "failure_cases_antipatterns_warnings", "source_specific_unique_insights",
        ]

    def make_documents(self):
        data = {name: [] for name in self.headings}
        # The user's safe 5th-chunk inspector reported 16 evidence rows.
        counts = {1: 1, 2: 2, 6: 2, 8: 1, 9: 2, 10: 1, 14: 4, 17: 2, 18: 1}
        serial = 201
        for index, name in enumerate(self.headings, 1):
            for _ in range(counts.get(index, 0)):
                serial += 1
                data[name].append({
                    "claim": f"source evidence {serial}",
                    "segment_ids": [f"s{serial:06d}"],
                })
        msg = {"status": "completed", "message": "PRIVATE_ACCOUNT_CONTENT_NOT_FOR_LOG"}
        placeholders = {key: "Unstructured model note" for key in data}
        return [data, msg, placeholders, placeholders]

    @staticmethod
    def envelope(docs):
        return json.dumps({
            "status": "SUCCESS",
            "response": "\n".join(
                json.dumps(doc, ensure_ascii=False) for doc in docs
            ),
        }, ensure_ascii=False)

    def test_strict_mode_remains_fail_closed(self):
        docs = self.make_documents()
        with self.assertRaisesRegex(RuntimeError, "AGY_(?:CATEGORY_SHAPE|SCHEMA_MISMATCH)"):
            parse_structured_response(self.envelope(docs), "", self.schema)

    def test_opt_in_quarantines_three_documents_without_dropping_raw(self):
        docs = self.make_documents()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            output, usage = parse_structured_response(
                self.envelope(docs), "", self.schema, quarantine_mixed=True
            )
        self.assertEqual(len(output["claims"]), 16)
        self.assertEqual(usage["quarantined_document_indices"], [2, 3, 4])
        self.assertEqual(len(usage["quarantined_document_sha256"]), 3)
        self.assertTrue(usage["review_required"])
        self.assertEqual(usage["recovered_document_index"], 1)
        self.assertEqual(usage["unrecognized_empty_category_slots"], [15])
        self.assertEqual(output["claims"][0]["segment_ids"], ["s000202"])
        self.assertNotIn("PRIVATE_ACCOUNT_CONTENT_NOT_FOR_LOG", errors.getvalue())
        self.assertIn("검수 대기", errors.getvalue())

    def test_opt_in_does_not_accept_nonmatching_status_or_nonidentical_strings(self):
        for change in ("status", "different_string", "embedded_json", "extra_document"):
            with self.subTest(change=change):
                docs = self.make_documents()
                if change == "status":
                    docs[1] = {"status": "cancelled", "message": "PRIVATE"}
                elif change == "different_string":
                    docs[3] = {**docs[3], "automation": "DIFFERENT PRIVATE CONTENT"}
                elif change == "embedded_json":
                    string_doc = dict(docs[2])
                    string_doc["automation"] = json.dumps([{
                        "claim": "MORE EVIDENCE", "segment_ids": ["s000220"]
                    }])
                    docs[2] = string_doc
                    docs[3] = dict(string_doc)
                else:
                    docs.append({"claims": []})
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(RuntimeError):
                        parse_structured_response(
                            self.envelope(docs), "", self.schema,
                            quarantine_mixed=True
                        )

    def test_opt_in_does_not_discard_unknown_nonempty_category(self):
        docs = self.make_documents()
        docs[0]["cost&tokens·performance"] = [{
            "claim": "UNTRUSTED EVIDENCE", "segment_ids": ["s000220"]
        }]
        with self.assertRaises(RuntimeError):
            parse_structured_response(
                self.envelope(docs), "", self.schema,
                quarantine_mixed=True
            )

    def test_opt_in_only_for_extraction_schema(self):
        docs = self.make_documents()
        other = {"properties": {"issues": {"type": "array"}}, "required": ["issues"]}
        with self.assertRaises(RuntimeError):
            parse_structured_response(
                self.envelope(docs), "", other, quarantine_mixed=True
            )


class AgyStreamingEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "type": "object",
            "properties": {"claims": {"type": "array"}},
            "required": ["claims"],
            "additionalProperties": False,
        }

    def test_terminal_result_is_used_not_partial_text_updates(self):
        stream = "\n".join([
            json.dumps({"event": "init", "init": {"model": "gemini-3.8-flash-medium"}}),
            json.dumps({"event": "step_update", "step_update": {
                "step_type": "agent_response", "text_delta": '{"reasoning":"UNSAFE-DO-NOT-USE"}'
            }}),
            json.dumps({"event": "step_update", "step_update": {
                "step_type": "agent_response", "text_delta": '{"claims":"invalid"}'
            }}),
            json.dumps({"event": "result", "result": {
                "status": "SUCCESS", "structured_output": {"claims": []},
                "response": '{"claims":[]}', "usage": {"output_tokens": 12}
            }})
        ]) + "\n"
        data, usage = parse_structured_response(stream, "", self.schema)
        self.assertEqual(data, {"claims": []})
        self.assertEqual(usage["output_tokens"], 12)

    def test_composite_terminal_response_can_use_last_valid_done(self):
        stream = "\n".join([
            json.dumps({"event": "init", "init": {}}),
            json.dumps({"event": "step_update", "step_update": {
                "step_type": "agent_response", "state": "DONE",
                "text_delta": '{"notes":"preliminary and not schema-valid"}',
            }}),
            json.dumps({"event": "step_update", "step_update": {
                "step_type": "agent_response", "state": "DONE",
                "text_delta": '{"claims":[{"section":1,"text":"A","segment_ids":["s000001"],"kind":"source_claim"}]}',
            }}),
            json.dumps({"event": "result", "result": {
                "status": "SUCCESS",
                "response": '{"claims":[{"section":1,"text":"A","segment_ids":["s000001"],"kind":"source_claim"}]}' + "\nAdditional commentary",
                "num_turns": 3,
                "usage": {"output_tokens": 200}
            }})
        ])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result, usage = parse_structured_response(stream, "", self.schema)
        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(usage["response_source"], "last_completed_agent_response")
        self.assertIn("마지막 완료 모델 응답", err.getvalue())

    def test_schema_mismatched_composite_may_recover_only_final_done(self):
        stream = "\n".join([
            json.dumps({"event": "init", "init": {}}),
            json.dumps({"event": "step_update", "step_update": {
                "step_type": "agent_response", "state": "DONE",
                "text_delta": '{"claims":[]}',
            }}),
            json.dumps({"event": "result", "result": {
                "status": "SUCCESS",
                "response": '{"notes":"intermediate"}\n{"claims":[]}',
                "num_turns": 2,
            }})
        ])
        with contextlib.redirect_stderr(io.StringIO()):
            data, usage = parse_structured_response(stream, "", self.schema)
        self.assertEqual(data, {"claims": []})
        self.assertEqual(usage["response_source"], "last_completed_agent_response")

    def test_invalid_final_done_must_fail_even_if_earlier_done_was_valid(self):
        stream = "\n".join([
            json.dumps({"event": "init", "init": {}}),
            json.dumps({"event": "step_update", "step_update": {
                "step_type": "agent_response", "state": "DONE",
                "text_delta": '{"claims":[]}',
            }}),
            json.dumps({"event": "step_update", "step_update": {
                "step_type": "agent_response", "state": "DONE",
                "text_delta": '{"notes":"not valid"}',
            }}),
            json.dumps({"event": "result", "result": {
                "status": "SUCCESS",
                "response": '{"claims":[]}\nextra',
                "num_turns": 2
            }})
        ])
        with self.assertRaisesRegex(RuntimeError, "AGY_UNSTRUCTURED_RESPONSE"):
            parse_structured_response(stream, "", self.schema)

    def test_no_done_agent_messages_does_not_discard_trailing_text(self):
        stream = "\n".join([
            json.dumps({"event": "init", "init": {}}),
            json.dumps({"event": "step_update", "step_update": {
                "step_type": "agent_response", "state": "ACTIVE",
                "text_delta": '{"claims":[]}',
            }}),
            json.dumps({"event": "result", "result": {
                "status": "SUCCESS", "response": '{"claims":[]}\ntext'
            }})
        ])
        with self.assertRaisesRegex(RuntimeError, "completed_agent_responses=0"):
            parse_structured_response(stream, "", self.schema)

    def test_missing_terminal_result_is_error(self):
        stream = "\n".join([
            json.dumps({"event": "init", "init": {}}),
            json.dumps({"event": "step_update", "step_update": {"state": "DONE"}})
        ])
        with self.assertRaisesRegex(RuntimeError, "AGY_STREAM_MISSING_RESULT"):
            parse_structured_response(stream, "", self.schema)

    def test_duplicate_or_trailing_events_after_result_are_rejected(self):
        stream = "\n".join([
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": '{"claims":[]}'}}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "ignored"}})
        ])
        with self.assertRaisesRegex(RuntimeError, "AGY_STREAM_TRAILING_EVENT"):
            parse_structured_response(stream, "", self.schema)

    def test_stream_terminal_result_schema_mismatch_fails(self):
        stream = "\n".join([
            json.dumps({"event": "init", "init": {}}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": '{"summary":"oops"}'}})
        ])
        with self.assertRaisesRegex(RuntimeError, "AGY_SCHEMA_MISMATCH"):
            parse_structured_response(stream, "", self.schema)


class AgyCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = self.root / "config"
        config.mkdir()
        (config / "agents.json").write_text(json.dumps({
            "extractor": {"model": "gemini-3.8-flash-medium", "effort": "medium"},
            "agy_agent": "yt-research-no-tools",
            "max_retries": 0,
        }), encoding="utf-8")
        agent_dir = self.root / ".agents" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "yt-research-no-tools.md").write_text(
            "---\nname: yt-research-no-tools\ntools: []\n---\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_uses_tool_free_agent_not_plan(self):
        response = json.dumps({
            "status": "SUCCESS",
            "structured_output": {"claims": []},
            "response": '{"claims":[]}',
            "usage": {"output_tokens": 5},
        })
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke", return_value=subprocess.CompletedProcess(
                 args=[], returncode=0, stdout=response, stderr=""
             )) as runner:
            data, usage = Agy(self.root).run(
                "extractor", "Only transform supplied text",
                {"type": "object", "properties": {}, "required": []},
            )
        command = runner.call_args.args[0]
        self.assertEqual(data, {"claims": []})
        self.assertEqual(usage["output_tokens"], 5)
        self.assertNotIn("--mode", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertEqual(command[command.index("--agent") + 1], "yt-research-no-tools")

    def test_missing_agent_definition_blocks_execution(self):
        (self.root / ".agents" / "agents" / "yt-research-no-tools.md").unlink()
        with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
             patch.object(Agy, "_invoke") as runner:
            with self.assertRaisesRegex(RuntimeError, "AGY_AGENT_MISSING"):
                Agy(self.root).run("extractor", "prompt", {})
        runner.assert_not_called()



class AgyMultiDocumentTests(unittest.TestCase):
    def test_repeated_complete_json_objects_are_accepted(self):
        pieces = '{"claims": []}\n{"claims": []}\n{"claims": []}\n'
        raw = json.dumps({
            "status": "SUCCESS", "response": pieces, "num_turns": 3,
            "usage": {"output_tokens": 100},
        })
        data, usage = parse_structured_response(raw, "")
        self.assertEqual(data, {"claims": []})
        self.assertEqual(usage["response_json_documents"], 3)

    def test_non_identical_claim_lists_preserve_distinct_claims(self):
        first = {"claims": [{"section": 1, "text": "A", "segment_ids": ["s000001"], "kind": "source_claim"}]}
        second = {"claims": [{"section": 1, "text": "B", "segment_ids": ["s000002"], "kind": "source_claim"}]}
        raw = json.dumps({"status": "SUCCESS", "response": json.dumps(first) + "\n" + json.dumps(second)})
        data, usage = parse_structured_response(raw, "")
        self.assertEqual(len(data["claims"]), 2)
        self.assertEqual(usage["response_json_documents"], 2)

    def test_trailing_diagnostics_do_not_expose_private_text(self):
        raw = json.dumps({
            "status": "SUCCESS",
            "response": '{"claims":[]}\nPRIVATE MODEL CONTENT NOT FOR LOGS',
        })
        with self.assertRaisesRegex(RuntimeError, "tail_kind=text_start") as caught:
            parse_structured_response(raw, "")
        error = str(caught.exception)
        self.assertIn("decoded_shapes=1=[claims:list(0)]", error)
        self.assertNotIn("PRIVATE MODEL CONTENT", error)

    def test_malformed_tail_cannot_be_silently_discarded(self):
        raw = json.dumps({"status": "SUCCESS", "response": '{"claims": []}\nINVALID'})
        with self.assertRaisesRegex(RuntimeError, "AGY_UNSTRUCTURED_RESPONSE"):
            parse_structured_response(raw, "")

    def test_conflicting_section_rewrites_are_rejected(self):
        raw = json.dumps({
            "status": "SUCCESS", "response": '{"sections":[{"number":1,"markdown":"A"}]}\n'
            '{"sections":[{"number":1,"markdown":"B"}]}',
        })
        with self.assertRaisesRegex(RuntimeError, "AGY_CONFLICTING_DOCUMENTS"):
            parse_structured_response(raw, "")

    def test_heartbeat_during_running_process(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config" / "agents.json").write_text(
                json.dumps({"heartbeat_seconds": 1, "cli_timeout_seconds": 60}), encoding="utf-8"
            )
            fake = MagicMock()
            fake.__enter__.return_value = fake
            fake.returncode = 0
            fake.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd="agy", timeout=1),
                (json.dumps({"status": "SUCCESS", "response": "{}"}), ""),
            ]
            with patch("ytresearch.agent.shutil.which", return_value="agy.exe"), \
                 patch("ytresearch.agent.subprocess.Popen", return_value=fake), \
                 contextlib.redirect_stderr(io.StringIO()) as output:
                runner = Agy(root)
                result = runner._invoke(["agy", "-p", "test"], "extractor", 1, 2)
            self.assertEqual(result.returncode, 0)
            self.assertIn("모델 처리 중", output.getvalue())
            self.assertIn("시도 1/2", output.getvalue())


class ProgressEncodingTests(unittest.TestCase):
    def test_progress_is_safe_on_cp1252(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252", write_through=True)
        with patch("sys.stdout", stream):
            report("[EXTRACT] 진행률 25% · 완료")
        stream.flush()
        text = buffer.getvalue().decode("cp1252")
        self.assertIn("25%", text)
        self.assertIn("EXTRACT", text)



if __name__ == "__main__":
    unittest.main()
