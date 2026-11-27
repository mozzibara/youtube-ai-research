"""Offline, synthetic-only WP1 contract tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ytresearch import research_batch as b


class ResearchBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source.mp4").write_bytes(b"synthetic media fixture")
        (self.root / "ref.txt").write_text("alpha beta", encoding="utf-8")
        self.model = "gemini-3.8-flash-medium"

    def prepare(self, **kwargs):
        with patch.object(b, "check_media", return_value={"duration_seconds": 30, "bytes": 23}):
            return b.prepare(self.root, "source.mp4", "ref.txt",
                             models=[self.model], **kwargs)

    def test_prepare_no_model_and_idempotent(self):
        with patch.object(b, "guided_run", side_effect=AssertionError("live forbidden")):
            bid = self.prepare()
            self.assertEqual(bid, self.prepare())
            root, base, plan = b.load_plan(self.root, bid)
            work, media = b.workspace(root, base, plan["jobs"][0], plan)
            self.assertEqual([p.name for p in work.iterdir()], ["sample.mp4"])
            self.assertEqual(media.read_bytes(), b"synthetic media fixture")
            self.assertEqual(b.report(root, bid)["reserved_attempts"], 0)

    def test_capture_report_and_duplicate_claim(self):
        bid = self.prepare(reference_kind="CAPTION_PROXY")
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        with self.assertRaises(FileExistsError):
            b.claim(root, base, job)
        self.assertEqual(b.report(root, bid)["jobs"][0]["status"], "BLOCKED_UNSETTLED_CLAIM")
        raw = b.json_bytes({"transcript": "alpha beta", "uncertainties": [], "status": "COMPLETE"})
        self.assertEqual(b.capture(root, base, job, raw, 2.0), "CAPTURED")
        result = b.report(root, bid)
        self.assertEqual(result["reference_kind"], "CAPTION_PROXY")
        self.assertEqual(result["jobs"][0]["metrics"]["raw_cer"]["rate"], 0)
        self.assertFalse(result["provider_ingress_verified"])
        self.assertEqual(result["reserved_attempts"], 1)
        self.assertEqual(b.report(root, bid), result)

    def test_unavailable_transcript_counts_as_missing_reference(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        raw = b.json_bytes({"transcript": "", "uncertainties": [], "status": "UNAVAILABLE"})
        self.assertEqual(b.capture(root, base, job, raw, 1), "CAPTURED")
        row = b.report(root, bid)["jobs"][0]
        self.assertFalse(row["transcript_nonempty"])
        self.assertEqual(row["metrics"]["raw_cer"]["rate"], 1)
        self.assertEqual(row["metrics"]["raw_wer"]["rate"], 1)

    def test_stage_mode_does_not_route_to_guided(self):
        with patch.object(b, "prepare", return_value="test"), patch.object(
                b, "stage_only", return_value={"script_submitted_model_requests": 0}) as stage, patch.object(
                b, "guided_run", side_effect=AssertionError("live forbidden")):
            self.assertEqual(b.main(["prepare", "--root", str(self.root),
                                     "--media", "source.mp4", "--stage-only"]), 0)
            stage.assert_called_once_with(str(self.root), "test")

    def test_invalid_capture_preserved(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        self.assertEqual(b.capture(root, base, job, b"not JSON", 1.0), "INVALID_CAPTURE")
        self.assertEqual((root / base / "j01/response.txt").read_bytes(), b"not JSON")
        self.assertEqual(b.report(root, bid)["jobs"][0]["status"], "INVALID_CAPTURE")

    def test_reference_and_response_tamper_blocked(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        b.capture(root, base, job, b'{"transcript":"alpha","uncertainties":[],"status":"PARTIAL"}', 1)
        path = root / base / "j01/response.txt"
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(b.BatchError, "CAPTURE_HASH_MISMATCH"):
            b.report(root, bid)
        (root / base / "evaluation-only/reference.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(b.BatchError, "REFERENCE_HASH_MISMATCH"):
            b.report(root, bid)

    def test_workspace_contamination_and_hash_tamper(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        work, media = b.workspace(root, base, job, plan)
        (work / "extra.txt").write_text("x")
        with self.assertRaisesRegex(b.BatchError, "WORKSPACE_CONTENT_MISMATCH"):
            b.workspace(root, base, job, plan)
        (work / "extra.txt").unlink()
        media.write_bytes(b"changed")
        with self.assertRaisesRegex(b.BatchError, "MEDIA_HASH_MISMATCH"):
            b.workspace(root, base, job, plan)

    def test_path_and_model_guard(self):
        with self.assertRaises(b.BatchError):
            b.safe_path(self.root, "../escape")
        with self.assertRaises(b.BatchError):
            b.safe_path(self.root, str(self.root / "source.mp4"))
        with self.assertRaisesRegex(b.BatchError, "MODEL_ALLOWLIST_REQUIRED"):
            b.prepare(self.root, "source.mp4", models=["unexpected"])
        (self.root / ".git").mkdir()
        with self.assertRaises(ValueError):
            b.safe_root(self.root)

    def test_response_validation_and_metrics(self):
        for invalid in (
            b'[]',
            b'{"transcript":"a","transcript":"b","uncertainties":[],"status":"COMPLETE"}',
            b'{"transcript":"","uncertainties":[],"status":"COMPLETE"}',
            b'{"transcript":"a","uncertainties":[],"status":"UNAVAILABLE"}',
            b'{"transcript":"a","uncertainties":[1],"status":"PARTIAL"}',
        ):
            with self.assertRaises(b.BatchError):
                b.validate_response(invalid)
        self.assertEqual(b.metrics("abc", "adc")["raw_cer"]["edits"], 1)
        self.assertEqual(b.metrics("a b.", "ab")["normalized_cer"]["rate"], 0)
        self.assertIsNone(b.error_rate("", "")["rate"])
        with patch.object(b, "MAX_EDIT_CELLS", 1):
            self.assertIsNone(b.error_rate("ab", "ab")["rate"])

    def test_default_probe_has_no_prompt(self):
        from subprocess import CompletedProcess
        def fake(cmd, **kwargs):
            self.assertIn(cmd[1], ("--help", "--version"))
            return CompletedProcess(cmd, 0, b"1.5.16 --model --effort --sandbox --dangerously-skip-permissions", b"")
        with patch.object(b.shutil, "which", return_value="synthetic-agy"), patch.object(
                b.subprocess, "run", side_effect=fake) as proc:
            info = b.capability_probe()
        self.assertEqual(proc.call_count, 2)
        self.assertEqual(info["model_requests"], 0)
        self.assertEqual(info["version"], "1.5.16")
        self.assertFalse(info["headless_media_verified"])

    def test_terminal_log_requires_unique_success_final(self):
        raw = b'{"transcript":"alpha","uncertainties":[],"status":"COMPLETE"}'
        result = b.json_bytes({"event":"result","result":{"status":"SUCCESS","response":raw.decode()}})
        # Real stream JSON events occupy one line each.
        event = json.dumps(json.loads(result), separators=(",", ":")).encode()
        self.assertEqual(b.terminal_log_candidate(event), raw)
        self.assertIsNone(b.terminal_log_candidate(event + b"\n" + event))
        self.assertIsNone(b.terminal_log_candidate(b'{"event":"step_update","step_update":{}}'))
        self.assertIsNone(b.terminal_log_candidate(b'{"event":"result","result":{"status":"ERROR"}}'))
        self.assertIsNone(b.terminal_log_candidate(b"debug\n" + event))
        self.assertEqual(b.compare_capture_candidate(raw, b" " + raw), "JSON_CONTENT_MATCH")
        self.assertEqual(b.compare_capture_candidate(raw, raw), "EXACT_TEXT_MATCH")

    def test_session_candidate_exact_single_turn_no_tools(self):
        raw = b'{"transcript":"alpha","uncertainties":[],"status":"COMPLETE"}'
        rows = [{"type":"USER_INPUT","content":b.PROMPT},
                {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE",
                 "content":raw.decode(),"tool_calls":[]}]
        encode = lambda: b"\n".join(json.dumps(row).encode() for row in rows)
        self.assertEqual(b.session_log_candidate(encode()), raw)
        rows[0]["content"] = "other prompt"
        self.assertIsNone(b.session_log_candidate(encode()))
        rows[0]["content"] = b.PROMPT
        rows[1]["tool_calls"] = [{"name":"read_file"}]
        self.assertIsNone(b.session_log_candidate(encode()))
        rows[1]["tool_calls"] = []
        rows[1]["status"] = "ACTIVE"
        self.assertIsNone(b.session_log_candidate(encode()))
        rows[1]["status"] = "DONE"
        rows.append(dict(rows[0]))
        self.assertIsNone(b.session_log_candidate(encode()))

    def test_log_probe_preserves_only_scoped_matched_session(self):
        folder = Path("private-job")
        (self.root / folder).mkdir()
        log = self.root / folder / "agy.log"
        raw = b'{"transcript":"alpha","uncertainties":[],"status":"COMPLETE"}'
        log.write_bytes(json.dumps({"event":"result","result":{
            "status":"SUCCESS","response":raw.decode()}}).encode())
        brain = self.root / "brain"
        (brain / "old").mkdir(parents=True)
        (brain / "new").mkdir()
        (brain / "old" / "transcript_full.jsonl").write_bytes(b"old private data")
        (brain / "new" / "transcript_full.jsonl").write_bytes(
            b"\n".join(json.dumps(row).encode() for row in (
                {"type":"USER_INPUT","content":b.PROMPT},
                {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","content":raw.decode()})))
        with patch.object(b, "fresh_brain_dirs", return_value=(brain, {"old","new"})):
            info = b.inspect_capture_sources(self.root, folder, log, (brain, {"old"}), raw)
        self.assertTrue(info["paired_log_capture_match"])
        self.assertFalse(info["automatic_response_capture_verified"])
        self.assertEqual(info["session_log"]["status"], "EXACT_TEXT_MATCH")
        self.assertNotIn(b"old private data", (self.root / folder / "session-log.snapshot").read_bytes())

    def test_recover_existing_capture_no_gui_or_model(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        raw = b.json_bytes({"transcript":"", "uncertainties":["unavailable"], "status":"UNAVAILABLE"})
        b.capture(root, base, job, raw, 1)
        log = root / base / job["id"] / ("agy-" + "a" * 32 + ".log")
        log.write_bytes(json.dumps({"event":"result","result":{
            "status":"SUCCESS","response":raw.decode()}}).encode())
        with patch.object(b, "gui_context", side_effect=AssertionError("GUI forbidden")), patch.object(
                b, "fresh_brain_dirs", side_effect=AssertionError("old session scan forbidden")), patch.object(
                b.subprocess, "run", side_effect=AssertionError("external process forbidden")):
            recovered = b.recover_log(root, bid, operator_closed=True)
            self.assertEqual(recovered, b.recover_log(root, bid, operator_closed=True))
        self.assertEqual(recovered["reserved_attempts"], 1)
        self.assertEqual(recovered["recovery"]["script_submitted_model_requests"], 0)
        self.assertTrue(recovered["log_probe"]["paired_log_capture_match"])
        self.assertFalse(recovered["jobs"][0]["transcript_nonempty"])
        self.assertEqual((root / base / "j01/response.txt").read_bytes(), raw)

    def test_recovery_missing_log_and_confirmation(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        with self.assertRaisesRegex(b.BatchError, "OPERATOR_CLOSE_CONFIRMATION_REQUIRED"):
            b.recover_log(root, bid, operator_closed=False)
        with self.assertRaisesRegex(b.BatchError, "EXISTING_CAPTURE_REQUIRED"):
            b.recover_log(root, bid, operator_closed=True)
        b.claim(root, base, job)
        b.capture(root, base, job, b.json_bytes({
            "transcript":"alpha", "uncertainties":[], "status":"COMPLETE"}), 1)
        info = b.recover_log(root, bid, operator_closed=True)
        self.assertEqual(info["log_probe"]["cli_log"]["status"], "MISSING")
        self.assertFalse(info["log_probe"]["paired_log_capture_match"])
        self.assertFalse(info["recovery"]["old_session_scan_performed"])

    def test_explicit_session_identifier_is_strict(self):
        token = "12345678-1234-1234-1234-123456789abc"
        self.assertEqual(b.explicit_session_ids(("session_id=" + token).encode()), {token})
        self.assertEqual(b.explicit_session_ids(("session_id mentioned " + token).encode()), set())
        self.assertEqual(b.explicit_session_ids(b"session_id=../../other"), set())

    def test_recover_session_from_persisted_baseline_no_external_call(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        folder = base / job["id"]
        brain = root / "brain"
        (brain / "old").mkdir(parents=True)
        (brain / "new").mkdir()
        raw = b.json_bytes({"transcript":"alpha", "uncertainties":[], "status":"COMPLETE"})
        trace = b"\n".join(json.dumps(row).encode() for row in (
            {"type":"USER_INPUT","content":b.PROMPT},
            {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","content":raw.decode()}))
        (brain / "new" / "transcript_full.jsonl").write_bytes(trace)
        (brain / "old" / "transcript_full.jsonl").write_bytes(b"private")
        with patch.object(b, "fresh_brain_dirs", return_value=(brain, {"old"})):
            b.persist_session_baseline(root, folder)
        b.claim(root, base, job)
        b.capture(root, base, job, raw, 1)
        with patch.object(b, "brain_root", return_value=brain), patch.object(
                b, "fresh_brain_dirs", return_value=(brain, {"old", "new"})), patch.object(
                b.subprocess, "run", side_effect=AssertionError("external process forbidden")):
            result = b.recover_session(root, bid, operator_closed=True)
        self.assertTrue(result["paired_log_capture_match"])
        self.assertEqual(result["candidate_logs_read"], 1)
        self.assertEqual(result["other_session_logs_read"], 0)
        self.assertEqual(result["script_submitted_model_requests"], 0)
        self.assertEqual(result["shape"]["matching_prompt_rows"], 1)
        self.assertEqual(result["shape"]["matching_response_rows"], 1)

    def test_recover_session_historical_missing_id_no_scan(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        b.capture(root, base, job, b.json_bytes({
            "transcript":"alpha", "uncertainties":[], "status":"COMPLETE"}), 1)
        with patch.object(b, "fresh_brain_dirs", side_effect=AssertionError("no scan")):
            result = b.recover_session(root, bid, operator_closed=True)
        self.assertEqual(result["status"], "NO_CORRELATED_SESSION")
        self.assertEqual(result["candidate_logs_read"], 0)

    def test_nested_duplicate_session_logs_are_not_ambiguous(self):
        session = self.root / "session"
        nested = session / "a" / "b"
        nested.mkdir(parents=True)
        folder = Path("private-job")
        raw = b.json_bytes({"transcript":"alpha","uncertainties":[],"status":"COMPLETE"})
        trace = b"\n".join(json.dumps(row).encode() for row in (
            {"type":"USER_INPUT","content":b.PROMPT},
            {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","content":raw.decode()}))
        for name in ("transcript.jsonl","transcript_full.jsonl"):
            (nested / name).write_bytes(trace)
        result = b.compare_scoped_session(self.root, folder, session, raw, fresh=True)
        self.assertEqual(result["candidate_logs_read"], 2)
        self.assertTrue(result["candidate_hashes_equal"])
        self.assertTrue(result["paired_log_capture_match"])
        self.assertEqual(result["candidates"][0]["depth"], 2)

    def test_nested_conflicting_session_logs_fail_closed(self):
        session = self.root / "session"
        nested = session / "a" / "b"
        nested.mkdir(parents=True)
        raw = b.json_bytes({"transcript":"alpha","uncertainties":[],"status":"COMPLETE"})
        trace = b"\n".join(json.dumps(row).encode() for row in (
            {"type":"USER_INPUT","content":b.PROMPT},
            {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","content":raw.decode()}))
        (nested / "transcript_full.jsonl").write_bytes(trace)
        (nested / "transcript.jsonl").write_bytes(b"not matching")
        folder = Path("private-job")
        result = b.compare_scoped_session(self.root, folder, session, raw, fresh=True)
        self.assertEqual(result["status"], "AMBIGUOUS_TRACE_CONTENT")
        self.assertFalse(result["paired_log_capture_match"])
        self.assertFalse((self.root / folder / "recovered-session.snapshot").exists())

    def test_metrics_reject_empty_normalized_reference(self):
        with self.assertRaisesRegex(b.BatchError, "REFERENCE_HAS_NO_NORMALIZED_CONTENT"):
            b.metrics("??? ???", "alpha")

    def test_observed_wrapper_and_fence_strict_live_contract(self):
        raw = b'{"transcript":"alpha","uncertainties":[],"status":"COMPLETE"}'
        user = {"type":"USER_INPUT","source":"USER_EXPLICIT","status":"DONE",
                "content":"synthetic wrapper\n"+b.PROMPT+"\nmetadata", "media":[{}]}
        model = {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE",
                 "content":"\x60\x60\x60json\n"+raw.decode()+"\n\x60\x60\x60"}
        encode = lambda: b"\n".join(json.dumps(row).encode() for row in (user, model))
        self.assertEqual(b.session_log_candidate(encode(), live=True), raw)
        self.assertEqual(b.session_log_candidate(encode()), raw)
        user["media"] = []
        self.assertIsNone(b.session_log_candidate(encode(), live=True))
        user["media"] = [{}]
        model["content"] += "\nextra"
        self.assertIsNone(b.session_log_candidate(encode()))
        model["content"] = raw.decode()
        user["content"] = b.PROMPT + b.PROMPT
        self.assertIsNone(b.session_log_candidate(encode(), live=True))

    def test_live_pair_requires_both_identical_and_done(self):
        session = self.root / "session"
        session.mkdir()
        raw = b'{"transcript":"alpha","uncertainties":[],"status":"COMPLETE"}'
        user = {"type":"USER_INPUT","source":"USER_EXPLICIT","status":"DONE",
                "content":b.PROMPT,"media":[{}]}
        model = {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","content":raw.decode()}
        trace = b"\n".join(json.dumps(row).encode() for row in (user, model))
        (session / "transcript_full.jsonl").write_bytes(trace)
        self.assertIsNone(b.live_pair(session))
        (session / "transcript.jsonl").write_bytes(trace)
        self.assertEqual(b.live_pair(session), (trace, raw))
        (session / "transcript.jsonl").write_bytes(trace+b"\n")
        self.assertIsNone(b.live_pair(session))
        model["status"] = "ACTIVE"
        trace = b"\n".join(json.dumps(row).encode() for row in (user, model))
        for name in ("transcript.jsonl","transcript_full.jsonl"):
            (session / name).write_bytes(trace)
        self.assertIsNone(b.live_pair(session))

    def test_live_capture_receipt_distinguishes_source(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        raw = b.json_bytes({"transcript":"alpha","uncertainties":[],"status":"COMPLETE"})
        b.capture(root, base, job, raw, 1, source="SESSION_TRACE_PILOT")
        self.assertEqual(b.report(root, bid)["jobs"][0]["capture_source"], "SESSION_TRACE_PILOT")

    def test_live_requires_explicit_paired_batch(self):
        with patch.object(b,"live_session_pilot",side_effect=AssertionError("live forbidden")):
            self.assertEqual(b.main(["prepare","--root",str(self.root),"--media","source.mp4",
                                     "--guided","--max-calls","1","--live-session"]), 2)

    def test_caption_proxy_window_keeps_intersections_and_marks_boundary(self):
        events = {"events":[
            {"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"outside"}]},
            {"tStartMs":59000,"dDurationMs":2000,"segs":[{"utf8":"left"}]},
            {"tStartMs":61000,"dDurationMs":2000,"segs":[{"utf8":"inside"}]},
            {"tStartMs":119000,"dDurationMs":2000,"segs":[{"utf8":"right"}]},
            {"tStartMs":120000,"dDurationMs":1000,"segs":[{"utf8":"outside"}]}]}
        text, info = b.caption_proxy_window(b.json_bytes(events), 60, 60)
        self.assertEqual(text, "left\ninside\nright")
        self.assertEqual(info["boundary_events"], 2)

    def test_live_batch_requires_authorization_before_any_work(self):
        with patch.object(b,"prepare_lecture",side_effect=AssertionError("no preparation")), patch.object(
                b,"live_batch",side_effect=AssertionError("no live")):
            rc = b.main(["lecture-batch","--root",str(self.root),"--source-root",str(self.root),
                         "--manifest","data/example.json","--paired-batch","x",
                         "--pilot-batch","y","--max-calls","6"])
        self.assertEqual(rc, 2)

    def test_completed_live_job_rejects_uncertain_capture(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root,bid)
        job = plan["jobs"][0]
        with self.assertRaisesRegex(b.BatchError, "UNSETTLED_OR_NONLIVE_CAPTURE_NO_RETRY"):
            b.completed_live_job(root,base,job,{"status":"BLOCKED_UNSETTLED_CLAIM"})
        b.claim(root,base,job)
        raw=b.json_bytes({"transcript":"alpha","uncertainties":[],"status":"COMPLETE"})
        b.capture(root,base,job,raw,1)
        with self.assertRaisesRegex(b.BatchError, "UNSETTLED_OR_NONLIVE_CAPTURE_NO_RETRY"):
            b.completed_live_job(root,base,job,b.report(root,bid)["jobs"][0])

    def test_live_batch_reuses_captured_job_and_stops_after_close_failure(self):
        models=list(b.MODELS)[:3]
        with patch.object(b,"check_media",return_value={"duration_seconds":30,"bytes":23}):
            bid=b.prepare(self.root,"source.mp4","ref.txt",models=models)
        root,base,plan=b.load_plan(self.root,bid)
        first=plan["jobs"][0]
        b.claim(root,base,first)
        raw=b.json_bytes({"transcript":"alpha","uncertainties":[],"status":"COMPLETE"})
        b.capture(root,base,first,raw,1,source="SESSION_TRACE_PILOT")
        calls=[]
        def fake_job(root,base,plan,job,pg,clip):
            calls.append(job["id"])
            b.claim(root,base,job)
            b.capture(root,base,job,raw,1,source="SESSION_TRACE_PILOT")
            return {"window_close_status":"MANUAL_CLOSE_REQUIRED"}
        # Patch a small predicate rather than os.name, which changes pathlib semantics.
        with patch.object(b,"is_windows",return_value=True), patch.object(
                b,"validate_live_proofs",return_value={}), patch.object(
                b,"capability_probe",return_value={"version":"1.5.16"}), patch.object(
                b,"gui_context",return_value=(None,None)), patch.object(
                b,"completed_live_job",return_value={}), patch.object(b,"live_job",side_effect=fake_job):
            result=b.live_batch(root,bid,"paired","pilot",3)
        self.assertEqual(calls,["j02"])
        self.assertEqual(result["reserved_attempts"],2)
        self.assertEqual(result["jobs"][2]["status"],"PENDING")

    def test_lecture_source_check_missing_manifest_and_integrity(self):
        (self.root / "data").mkdir()
        rel = "data/example.json"
        info = b.lecture_source_check(self.root, rel)
        self.assertEqual(info["error_code"], "MANIFEST_NOT_FOUND")
        asset = self.root / "source.mp4"
        mf = {"schema_version": "verbatim_single_v1", "video_id": "abcdefghijk",
              "assets": {"primary_video": {"path": "source.mp4",
                         "size_bytes": asset.stat().st_size, "sha256": b.sha(asset)}}}
        (self.root / rel).write_bytes(b.json_bytes(mf))
        with patch.object(b.subprocess, "run", side_effect=AssertionError("no process")), patch.object(
                b, "gui_context", side_effect=AssertionError("no GUI")):
            info = b.lecture_source_check(self.root, rel)
            self.assertEqual(info["status"], "PASS")
            self.assertEqual(info["verified_asset_roles"], ["primary_video"])
            self.assertEqual(info["model_requests"], 0)
            asset.write_bytes(b"X" * asset.stat().st_size)
            info = b.lecture_source_check(self.root, rel)
            self.assertEqual(info["error_code"], "ASSET_HASH_MISMATCH")
            self.assertEqual(info["status"], "BLOCKED")

    def test_verbatim_error_redaction_and_cli(self):
        import contextlib
        import io
        self.assertEqual(b.public_error_code(b.VerbatimError("ASSET_SIZE_MISMATCH")),
                         "ASSET_SIZE_MISMATCH")
        self.assertEqual(b.public_error_code(b.VerbatimError("secret path")),
                         "VERBATIM_VALIDATION_FAILED")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
                b, "live_batch", side_effect=AssertionError("no model")):
            rc = b.main(["lecture-source-check", "--source-root", str(self.root),
                         "--manifest", "data/absent.json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "MANIFEST_NOT_FOUND")
        error = io.StringIO()
        with contextlib.redirect_stderr(error), patch.object(
                b, "validate_live_proofs", return_value={}), patch.object(
                b, "prepare_lecture", side_effect=b.VerbatimError("ASSET_OUTSIDE_RUNTIME")), patch.object(
                b, "live_batch", side_effect=AssertionError("no model")):
            rc = b.main(["lecture-batch", "--root", str(self.root), "--source-root",
                         str(self.root), "--manifest", "data/example.json",
                         "--paired-batch", "x", "--pilot-batch", "y",
                         "--max-calls", "6", "--authorize-guided"])
        self.assertEqual(rc, 2)
        self.assertIn("WP1_BLOCKED:ASSET_OUTSIDE_RUNTIME", error.getvalue())

    def numeric_legacy_fixture(self):
        (self.root / "data").mkdir()
        (self.root / "research").mkdir()
        rows = [{"video_id": f"v{i:010d}", "playlist_position": i,
                 "source_id": f"S{i:03d}"} for i in range(1, 78)]
        ledger = self.root / "research" / "index.json"
        ledger.write_bytes(b.json_bytes({"source_count": 77, "sources": rows}))
        asset = self.root / "source.mp4"
        mf = {"schema_version": 1, "status": "OFFLINE_INPUT_VERIFIED",
              "video_id": rows[0]["video_id"],
              "assets": {role: {"path": "source.mp4", "size_bytes": asset.stat().st_size,
                                "sha256": b.sha(asset)}
                         for role in ("primary_video", "highres_video", "audio", "subtitle_json3")}}
        path = self.root / "data" / "pilot.json"
        path.write_bytes(b.json_bytes(mf))
        return path, mf, ledger

    def test_lecture_numeric_legacy_v1_preserves_original_and_default_contract(self):
        path, mf, ledger = self.numeric_legacy_fixture()
        original = path.read_bytes()
        with patch.object(b.subprocess, "run", side_effect=AssertionError("no process")), patch.object(
                b, "gui_context", side_effect=AssertionError("no GUI")):
            result = b.lecture_source_check(self.root, "data/pilot.json")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["manifest_contract"], "LEGACY_NUMERIC_V1")
        self.assertEqual(result["verified_asset_count"], 4)
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaisesRegex(b.VerbatimError, "SCHEMA_UNSUPPORTED"):
            b._load_validated_context(self.root.resolve(), path.resolve())

    def test_lecture_numeric_legacy_v1_requires_registry_roles_and_hashes(self):
        path, mf, ledger = self.numeric_legacy_fixture()
        original = ledger.read_bytes()
        ledger.unlink()
        self.assertEqual(b.lecture_source_check(self.root, "data/pilot.json")["error_code"],
                         "LEGACY_SOURCE_LEDGER_MISSING")
        ledger.write_bytes(original)
        mf["video_id"] = "unregisterd"
        path.write_bytes(b.json_bytes(mf))
        self.assertEqual(b.lecture_source_check(self.root, "data/pilot.json")["error_code"],
                         "VIDEO_NOT_REGISTERED")
        mf["video_id"] = "v0000000001"
        audio = mf["assets"].pop("audio")
        path.write_bytes(b.json_bytes(mf))
        self.assertEqual(b.lecture_source_check(self.root, "data/pilot.json")["error_code"],
                         "ASSET_ROLES_INVALID")
        mf["assets"]["audio"] = audio
        audio["sha256"] = "0" * 64
        path.write_bytes(b.json_bytes(mf))
        self.assertEqual(b.lecture_source_check(self.root, "data/pilot.json")["error_code"],
                         "ASSET_HASH_MISMATCH")

    def test_lecture_numeric_legacy_v1_rejects_ambiguous_schema_and_status(self):
        path, mf, ledger = self.numeric_legacy_fixture()
        for unsupported in (True, 1.0, "1", 2):
            mf["schema_version"] = unsupported
            path.write_bytes(b.json_bytes(mf))
            self.assertEqual(b.lecture_source_check(self.root, "data/pilot.json")["error_code"],
                             "SCHEMA_UNSUPPORTED")
        mf["schema_version"] = 1
        mf["contract"] = "verbatim_single_v1"
        path.write_bytes(b.json_bytes(mf))
        self.assertEqual(b.lecture_source_check(self.root, "data/pilot.json")["error_code"],
                         "SCHEMA_CONFLICT")
        del mf["contract"]
        mf["status"] = "UNVERIFIED"
        path.write_bytes(b.json_bytes(mf))
        self.assertEqual(b.lecture_source_check(self.root, "data/pilot.json")["error_code"],
                         "LEGACY_V1_STATUS_INVALID")

    def external_media_fixture(self):
        path, mf, ledger = self.numeric_legacy_fixture()
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        approved = Path(external.name).resolve() / "videos"
        media = approved / mf["video_id"] / "media"
        media.mkdir(parents=True)
        for role, ext in (("primary_video",".mp4"), ("highres_video",".mp4"),
                          ("audio",".m4a"), ("subtitle_json3",".json3")):
            src = media / (role + ext)
            src.write_bytes(role.encode())
            mf["assets"][role] = {"path": str(src), "size_bytes": src.stat().st_size,
                                  "sha256": b.sha(src)}
        path.write_bytes(b.json_bytes(mf))
        return path, mf, approved

    def test_lecture_import_preserves_boundary_original_and_hashes(self):
        path, mf, approved = self.external_media_fixture()
        original = path.read_bytes()
        self.assertEqual(b.lecture_source_check(self.root,"data/pilot.json")["error_code"],
                         "ASSET_OUTSIDE_RUNTIME")
        with patch.object(b.subprocess,"run",side_effect=AssertionError("no process")), patch.object(
                b,"gui_context",side_effect=AssertionError("no GUI")):
            result = b.import_lecture_assets(self.root,"data/pilot.json",approved)
            self.assertEqual(result,b.import_lecture_assets(self.root,"data/pilot.json",approved))
        self.assertEqual(result["verified_asset_count"],4)
        self.assertEqual(result["model_requests"],0)
        self.assertEqual(path.read_bytes(),original)
        ctx = b.lecture_source_context(self.root,result["manifest"])
        for role, item in mf["assets"].items():
            self.assertTrue(ctx.found[role].is_relative_to(self.root.resolve()))
            self.assertEqual(b.sha(ctx.found[role]),item["sha256"])
            self.assertEqual(b.sha(Path(item["path"])),item["sha256"])

    def test_lecture_import_rejects_wrong_source_and_tampered_asset_before_copy(self):
        path,mf,approved = self.external_media_fixture()
        mf["assets"]["audio"]["path"] = str(self.root / "source.mp4")
        path.write_bytes(b.json_bytes(mf))
        with self.assertRaisesRegex(b.BatchError,"IMPORT_ASSET_OUTSIDE_APPROVED_VIDEO_MEDIA"):
            b.import_lecture_assets(self.root,"data/pilot.json",approved)
        mf["assets"]["audio"]["path"] = str(approved / mf["video_id"] / "media" / "audio.m4a")
        mf["assets"]["audio"]["sha256"] = "0"*64
        path.write_bytes(b.json_bytes(mf))
        with self.assertRaisesRegex(b.BatchError,"ASSET_HASH_MISMATCH"):
            b.import_lecture_assets(self.root,"data/pilot.json",approved)
        self.assertFalse((self.root / "data/wp2-imports").exists())

    def test_file_drop_retries_only_local_helper(self):
        from types import SimpleNamespace
        fail = SimpleNamespace(returncode=4, stdout=b"FILE_DROP_SET_OR_READ_FAILED\n")
        ok = SimpleNamespace(returncode=0, stdout=b"FILE_DROP_READY\r\n")
        with patch.object(b.subprocess, "run", side_effect=[fail, fail, ok]) as run, patch.object(
                b.time, "sleep"):
            b.prepare_file_drop("helper", "media")
        self.assertEqual(run.call_count, 3)
        invalid = SimpleNamespace(returncode=2, stdout=b"FILE_DROP_INPUT_INVALID\n")
        with patch.object(b.subprocess, "run", return_value=invalid) as run:
            with self.assertRaisesRegex(b.BatchError, "CLIPBOARD_FILE_DROP_INPUT_INVALID"):
                b.prepare_file_drop("helper", "media")
        self.assertEqual(run.call_count, 1)

    def test_clipboard_recovery_preserves_claim_and_is_one_time(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        folder = base / job["id"]
        brain = root / "brain"
        (brain / "new").mkdir(parents=True)
        b.claim(root, base, job)
        b.write_once(root, folder / "AGY_WP1_0123456789AB.ps1", b"synthetic")
        with patch.object(b, "fresh_brain_dirs", return_value=(brain, set())):
            b.persist_session_baseline(root, folder)
        old = b.read_bytes(b.safe_path(root, folder / "claim.json"))
        with patch("tools.agy_media_probe.windows", return_value=[]), patch.object(
                b, "fresh_brain_dirs", return_value=(brain, {"new"})):
            b.authorize_clipboard_recovery(root, base, plan, job, None)
            with self.assertRaisesRegex(b.BatchError, "CLIPBOARD_RECOVERY_ALREADY_USED"):
                b.authorize_clipboard_recovery(root, base, plan, job, None)
        self.assertEqual(b.read_bytes(b.safe_path(root, folder / "claim.json")), old)
        self.assertEqual(b.report(root, bid)["reserved_attempts"], 1)

    def test_clipboard_recovery_blocks_any_nonempty_trace(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        folder = base / job["id"]
        brain = root / "brain"
        (brain / "new").mkdir(parents=True)
        (brain / "new" / "transcript.jsonl").write_bytes(b"nonempty")
        b.claim(root, base, job)
        b.write_once(root, folder / "AGY_WP1_0123456789AB.ps1", b"synthetic")
        with patch.object(b, "fresh_brain_dirs", return_value=(brain, set())):
            b.persist_session_baseline(root, folder)
        with patch("tools.agy_media_probe.windows", return_value=[]), patch.object(
                b, "fresh_brain_dirs", return_value=(brain, {"new"})):
            with self.assertRaisesRegex(b.BatchError, "CLIPBOARD_RECOVERY_SESSION_TRACE_PRESENT"):
                b.authorize_clipboard_recovery(root, base, plan, job, None)
        self.assertFalse(b.safe_path(root, folder / "clipboard-recovery.json").exists())

    def test_agreeing_trace_aliases_accept_completed_intermediate_planner_only(self):
        session = self.root / "session"
        session.mkdir()
        raw = b'{"transcript":"alpha","uncertainties":[],"status":"COMPLETE"}'
        user = {"type":"USER_INPUT","source":"USER_EXPLICIT","status":"DONE",
                "content":b.PROMPT,"media":[{}]}
        final = {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","content":raw.decode()}
        thought = dict(final, content="synthetic intermediate text")
        encode = lambda rows: b"\n".join(b.json_bytes(row).strip().replace(b"\n", b" ") for row in rows)
        a, c = encode([user,thought,final]), encode([user,final])
        (session / "transcript_full.jsonl").write_bytes(a)
        (session / "transcript.jsonl").write_bytes(c)
        pair, info = b.read_agreeing_pair(session)
        self.assertEqual(pair, (a, raw, c))
        self.assertFalse(info["hashes_equal"])
        (session / "transcript_full.jsonl").write_bytes(encode([user,final,final]))
        self.assertIsNone(b.read_agreeing_pair(session)[0])
        (session / "transcript_full.jsonl").write_bytes(encode([user,dict(thought,tool_calls=[{}]),final]))
        self.assertIsNone(b.read_agreeing_pair(session)[0])
        (session / "transcript_full.jsonl").write_bytes(encode([user,dict(thought,type="GENERIC"),final]))
        self.assertIsNone(b.read_agreeing_pair(session)[0])

    def test_recover_interrupted_live_job_uses_recovery_baseline_no_submission(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        folder = base / job["id"]
        brain = root / "brain"
        (brain / "abandoned").mkdir(parents=True)
        (brain / "new").mkdir()
        b.claim(root, base, job)
        with patch.object(b, "fresh_brain_dirs", return_value=(brain, set())):
            b.persist_session_baseline(root, folder)
        old = b.read_bytes(b.safe_path(root, folder / "session-baseline.json"))
        b.write_once(root, folder / "clipboard-recovery.json",
                     b.json_bytes({"job_id":job["id"],"prior_baseline_sha256":b.digest(old)}))
        with patch.object(b, "fresh_brain_dirs", return_value=(brain, {"abandoned"})):
            b.persist_session_baseline(root, folder, filename="session-baseline-recovery.json")
        raw = b'{"transcript":"alpha","uncertainties":[],"status":"COMPLETE"}'
        trace = b"\n".join(json.dumps(row).encode() for row in (
            {"type":"USER_INPUT","source":"USER_EXPLICIT","status":"DONE","content":b.PROMPT,"media":[{}]},
            {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","content":raw.decode()}))
        for name in ("transcript_full.jsonl","transcript.jsonl"):
            (brain / "new" / name).write_bytes(trace)
        (brain / "abandoned" / "transcript_full.jsonl").write_bytes(b"private")
        with patch.object(b,"brain_root",return_value=brain), patch.object(
                b,"fresh_brain_dirs",return_value=(brain,{"new","abandoned"})), patch.object(
                b,"gui_context",side_effect=AssertionError("no GUI")), patch.object(
                b.subprocess,"run",side_effect=AssertionError("no external process")):
            result = b.recover_live_job(root,bid,job["id"],operator_closed=True)
            self.assertEqual(result["status"],"CAPTURED")
            self.assertEqual(result["model_requests"],0)
            self.assertEqual(result["baseline_kind"],"CLIPBOARD_RECOVERY")
            self.assertEqual(b.recover_live_job(root,bid,job["id"],operator_closed=True)["status"],
                             "ALREADY_CAPTURED")
        row=b.report(root,bid)["jobs"][0]
        self.assertIsNone(row["elapsed_seconds"])
        self.assertEqual(row["capture_source"],"SESSION_TRACE_RECOVERY")
        self.assertEqual(b.report(root,bid)["reserved_attempts"],1)

    def test_operator_reconstruction_is_distinct_and_reusable_without_trace(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        folder = base / job["id"]
        brain = root / "brain"
        brain.mkdir()
        b.claim(root, base, job)
        with patch.object(b,"fresh_brain_dirs",return_value=(brain,set())):
            b.persist_session_baseline(root,folder)
        supplied = b.json_bytes({"transcript":"alpha","uncertainties":[],"status":"COMPLETE"})
        b.write_once(root,Path("supplied.json"),supplied)
        with patch.object(b,"brain_root",return_value=brain), patch.object(
                b,"fresh_brain_dirs",return_value=(brain,set())), patch.object(
                b.subprocess,"run",side_effect=AssertionError("no external process")):
            result=b.recover_live_job(root,bid,job["id"],operator_closed=True,
                                      operator_response="supplied.json")
        self.assertEqual(result["status"],"CAPTURED")
        self.assertEqual(result["capture_source"],"OPERATOR_TUI_RECONSTRUCTION")
        row=b.report(root,bid)["jobs"][0]
        self.assertIsNone(row["elapsed_seconds"])
        evidence=b.completed_live_job(root,base,job,row)
        self.assertFalse(evidence["original_provider_bytes_verified"])
        self.assertFalse(evidence["automatic_session_extraction_observed"])

    def test_operator_reconstruction_rejects_conflicting_valid_trace(self):
        bid=self.prepare()
        root,base,plan=b.load_plan(self.root,bid)
        job=plan["jobs"][0]
        folder=base / job["id"]
        brain=root / "brain"
        (brain / "new").mkdir(parents=True)
        b.claim(root,base,job)
        with patch.object(b,"fresh_brain_dirs",return_value=(brain,set())):
            b.persist_session_baseline(root,folder)
        supplied=b.json_bytes({"transcript":"alpha","uncertainties":[],"status":"COMPLETE"})
        other=b.json_bytes({"transcript":"different","uncertainties":[],"status":"COMPLETE"})
        b.write_once(root,Path("supplied.json"),supplied)
        trace=b"\n".join(json.dumps(row).encode() for row in (
            {"type":"USER_INPUT","source":"USER_EXPLICIT","status":"DONE","content":b.PROMPT,"media":[{}]},
            {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE","content":other.decode()}))
        (brain / "new" / "transcript_full.jsonl").write_bytes(trace)
        with patch.object(b,"brain_root",return_value=brain), patch.object(
                b,"fresh_brain_dirs",return_value=(brain,{"new"})):
            with self.assertRaisesRegex(b.BatchError,"OPERATOR_RECONSTRUCTION_TRACE_CONFLICT"):
                b.recover_live_job(root,bid,job["id"],operator_closed=True,operator_response="supplied.json")
        self.assertFalse(b.safe_path(root,folder / "response.txt").exists())

    def length_fixture(self):
        # No FFmpeg, GUI, provider, or real media in this contract fixture.
        def prepare_clip(root, source_root, manifest, start, seconds, models=None, trial="lecture-1"):
            media = Path(f"fixture-{start}-{seconds}.mp4")
            ref = Path(f"fixture-{start}-{seconds}.txt")
            b.write_once(root, media, f"synthetic {start} {seconds}".encode())
            # Identical total reference across tilings, including spaces.
            b.write_once(root, ref, ("alpha " * (seconds // 30)).strip().encode())
            with patch.object(b, "check_media", return_value={
                    "duration_seconds": seconds, "bytes": (root / media).stat().st_size}):
                bid = b.prepare(root, media, ref, "CAPTION_PROXY", models, trial)
            _, base, plan = b.load_plan(root, bid)
            meta = {"batch_id": bid, "clip_sha256": plan["media_sha256"],
                    "start_seconds": start, "duration_seconds": seconds,
                    "source_manifest_assets_verified": True}
            b.write_once(root, base / "lecture-source.json", b.json_bytes(meta))
            return bid, meta

        original, _ = prepare_clip(self.root, self.root, "manifest", 60, 60,
                                    [b.LENGTH_MODEL], "original")
        root, base, plan = b.load_plan(self.root, original)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        b.capture(root, base, job, b.json_bytes({
            "transcript": "alpha alpha", "uncertainties": [], "status": "COMPLETE"}), 2)
        return original, prepare_clip

    def test_equal_span_length_plan_reuses_one_and_scores_common_reference(self):
        original, prepare_clip = self.length_fixture()
        with patch.object(b, "prepare_lecture", side_effect=prepare_clip), patch.object(
                b, "completed_live_job", return_value={}):
            result = b.prepare_length_plan(self.root, self.root, "manifest", 60, original)
            self.assertEqual(result["pending_calls"], 6)
            self.assertEqual([g["clip_count"] for g in result["groups"]], [4, 2, 1])
            self.assertEqual(result, b.prepare_length_plan(self.root, self.root, "manifest", 60, original))
            for group in result["groups"]:
                for window in group["windows"]:
                    if window["reused"]:
                        self.assertEqual(window["batch_id"], original)
                        continue
                    root, base, plan = b.load_plan(self.root, window["batch_id"])
                    job = plan["jobs"][0]
                    b.claim(root, base, job)
                    b.capture(root, base, job, b.json_bytes({
                        "transcript": ("alpha " * (window["duration_seconds"] // 30)).strip(),
                        "uncertainties": [], "status": "COMPLETE"}), 3)
            final = b.length_report(self.root, result["length_plan"])
            self.assertEqual(final["pending_calls"], 0)
            self.assertEqual(final["status"], "COMPLETE")
            for group in final["groups"]:
                self.assertEqual(group["common_span_metrics"]["normalized_cer"]["rate"], 0)
                self.assertEqual(group["common_span_metrics"]["normalized_cer"]["reference_units"], 20)

    def test_equal_span_length_plan_blocks_unsettled_and_budget(self):
        original, prepare_clip = self.length_fixture()
        with patch.object(b, "prepare_lecture", side_effect=prepare_clip), patch.object(
                b, "completed_live_job", return_value={}), patch.object(
                b, "live_batch", side_effect=AssertionError("no calls")):
            result = b.prepare_length_plan(self.root, self.root, "manifest", 60, original)
            lid = result["length_plan"]
            with self.assertRaisesRegex(b.BatchError, "GUIDED_AUTHORIZATION_REQUIRED"):
                b.run_length_plan(self.root, lid, "paired", "pilot", 6, authorized=False)
            with self.assertRaisesRegex(b.BatchError, "LENGTH_PENDING_EXCEEDS_APPROVED_LIMIT"):
                b.run_length_plan(self.root, lid, "paired", "pilot", 5, authorized=True)
            root, base, plan = b.load_plan(self.root, result["pending_batches"][0])
            b.claim(root, base, plan["jobs"][0])
            with self.assertRaisesRegex(b.BatchError, "LENGTH_UNSETTLED_CLAIM_NO_RETRY"):
                b.length_report(root, lid)

    def test_equal_span_length_plan_refuses_wrong_reuse_window(self):
        original, prepare_clip = self.length_fixture()
        with patch.object(b, "prepare_lecture", side_effect=prepare_clip), patch.object(
                b, "completed_live_job", return_value={}):
            with self.assertRaisesRegex(b.BatchError, "LENGTH_REUSE_SOURCE_MISMATCH"):
                b.prepare_length_plan(self.root, self.root, "manifest", 90, original)

    def complete_length_fixture(self, result):
        for group in result["groups"]:
            for window in group["windows"]:
                if window["capture_status"] == "CAPTURED":
                    continue
                root, base, plan = b.load_plan(self.root, window["batch_id"])
                job = plan["jobs"][0]
                b.claim(root, base, job)
                b.capture(root, base, job, b.json_bytes({
                    "transcript": ("alpha " * (window["duration_seconds"] // 30)).strip(),
                    "uncertainties": [], "status": "COMPLETE"}), 3)

    def test_long_equal_span_reuses_short_plan_and_scores_tail(self):
        original, prepare_clip = self.length_fixture()
        with patch.object(b, "prepare_lecture", side_effect=prepare_clip), patch.object(
                b, "completed_live_job", return_value={}), patch.object(
                b, "live_batch", side_effect=AssertionError("no provider")):
            prior = b.prepare_length_plan(self.root, self.root, "manifest", 60, original)
            with self.assertRaisesRegex(b.BatchError, "LONG_LENGTH_PRIOR_PLAN_INCOMPLETE"):
                b.prepare_long_length_plan(self.root, self.root, "manifest", prior["length_plan"])
            self.complete_length_fixture(prior)
            result = b.prepare_long_length_plan(self.root, self.root, "manifest", prior["length_plan"])
            self.assertEqual(result["span_seconds"], 300)
            self.assertEqual(result["pending_calls"], 3)
            self.assertEqual([[w["duration_seconds"] for w in g["windows"]]
                              for g in result["groups"]], [[120, 120, 60], [300]])
            self.assertEqual(result["groups"][0]["windows"][0]["batch_id"],
                             prior["groups"][-1]["windows"][0]["batch_id"])
            self.assertEqual(result, b.prepare_long_length_plan(
                self.root, self.root, "manifest", prior["length_plan"]))
            with self.assertRaisesRegex(b.BatchError, "LONG_LENGTH_CALL_LIMIT_INVALID"):
                b.run_length_plan(self.root, result["length_plan"], "paired", "pilot", 4,
                                  authorized=True)
            self.complete_length_fixture(result)
            final = b.length_report(self.root, result["length_plan"])
            self.assertEqual(final["status"], "COMPLETE")
            for group in final["groups"]:
                self.assertEqual(group["common_span_metrics"]["normalized_cer"]["rate"], 0)
                self.assertEqual(group["common_span_metrics"]["normalized_cer"]["reference_units"], 50)
            self.assertEqual(b.length_report(self.root, prior["length_plan"])["span_seconds"], 120)

    def test_long_equal_span_refuses_tampered_tail_and_unsettled_claim(self):
        original, prepare_clip = self.length_fixture()
        with patch.object(b, "prepare_lecture", side_effect=prepare_clip), patch.object(
                b, "completed_live_job", return_value={}):
            prior = b.prepare_length_plan(self.root, self.root, "manifest", 60, original)
            self.complete_length_fixture(prior)
            result = b.prepare_long_length_plan(self.root, self.root, "manifest", prior["length_plan"])
            root, plan = b.load_length_plan(self.root, result["length_plan"])
            plan["groups"][0]["windows"][-1]["duration_seconds"] = 120
            wrong = b.length_id(plan)
            b.write_once(root, Path("length-plans") / (wrong + ".json"), b.json_bytes(plan))
            with self.assertRaisesRegex(b.BatchError, "LENGTH_COVERAGE_INVALID"):
                b.load_length_plan(root, wrong)
            root, base, batch = b.load_plan(root, result["pending_batches"][0])
            b.claim(root, base, batch["jobs"][0])
            with self.assertRaisesRegex(b.BatchError, "LENGTH_UNSETTLED_CLAIM_NO_RETRY"):
                b.length_report(root, result["length_plan"])

    def test_exact_boundary_merge_unique_and_ambiguous(self):
        right = "alpha bravo charlie delta"
        part, info = b.exact_boundary_merge("before alpha bravo charlie", right)
        self.assertEqual(part, "delta")
        self.assertEqual(info["removed_words"], 3)
        repeated = "alpha alpha alpha alpha alpha"
        self.assertEqual(b.exact_boundary_merge(repeated, repeated)[1]["status"], "AMBIGUOUS_MATCH")
        self.assertEqual(b.exact_boundary_merge("alpha bravo", "alpha bravo")[1]["removed_words"], 0)

    def test_overlap_plan_reuses_baseline_and_scores_without_calls(self):
        original, prepare_clip = self.length_fixture()
        with patch.object(b, "prepare_lecture", side_effect=prepare_clip), patch.object(
                b, "completed_live_job", return_value={}), patch.object(
                b, "live_batch", side_effect=AssertionError("no provider")):
            prior = b.prepare_length_plan(self.root, self.root, "manifest", 60, original)
            self.complete_length_fixture(prior)
            long_plan = b.prepare_long_length_plan(self.root, self.root, "manifest", prior["length_plan"])
            self.complete_length_fixture(long_plan)
            result = b.prepare_overlap_plan(self.root, self.root, "manifest", long_plan["length_plan"])
            self.assertEqual(result["pending_calls"], 4)
            self.assertEqual([v["total_media_seconds"] for v in result["variants"]], [300,310,320])
            self.assertEqual([v["captured_clips"] for v in result["variants"]], [3,1,1])
            self.assertEqual(result, b.prepare_overlap_plan(self.root, self.root, "manifest", long_plan["length_plan"]))
            with self.assertRaisesRegex(b.BatchError, "OVERLAP_PENDING_EXCEEDS_APPROVED_LIMIT"):
                b.run_overlap_plan(self.root, result["overlap_plan"], "paired", "pilot", 3, authorized=True)
            # Fill new fixtures with deterministic transcripts.
            for bid in result["pending_batches"]:
                root, base, batch = b.load_plan(self.root, bid)
                job = batch["jobs"][0]
                b.claim(root, base, job)
                b.capture(root, base, job, b.json_bytes({
                    "transcript": "alpha alpha alpha alpha",
                    "uncertainties": [], "status": "COMPLETE"}), 3)
            final = b.overlap_report(self.root, result["overlap_plan"])
            self.assertEqual(final["status"], "COMPLETE")
            self.assertEqual(final["pending_calls"], 0)
            self.assertEqual(final["variants"][0]["raw_concatenation_metrics"]["normalized_cer"]["rate"], 0)
            self.assertEqual(final["variants"][0]["raw_concatenation_metrics"],
                             final["variants"][0]["exact_boundary_merge_metrics"])
            for variant in final["variants"]:
                self.assertEqual(variant["exact_boundary_merge_metrics"]["normalized_cer"]["reference_units"], 50)

    def test_context_terminal_requires_single_success_and_agreement(self):
        response = {"transcript": "alpha", "uncertainties": [], "status": "COMPLETE"}
        terminal = {"event": "result", "result": {"status": "SUCCESS",
                    "structured_output": response, "response": json.dumps(response)}}
        raw = json.dumps(terminal).encode()
        self.assertEqual(b.context_terminal_response(raw), response)
        with self.assertRaisesRegex(b.BatchError, "CONTEXT_STREAM_INVALID"):
            b.context_terminal_response(raw + b"\n" + raw)
        terminal["result"]["response"] = json.dumps(dict(response, transcript="other"))
        with self.assertRaisesRegex(b.BatchError, "CONTEXT_TERMINAL_RESPONSE_CONFLICT"):
            b.context_terminal_response(json.dumps(terminal).encode())

    def test_context_repeated_identical_documents_and_strict_rejection(self):
        response = {"transcript": "alpha", "uncertainties": [], "status": "COMPLETE"}
        obj = json.dumps(response)
        def raw(text, **extra):
            return b.json_bytes({"event": "result", "result": {
                "status": "SUCCESS", "response": text, **extra}})
        self.assertEqual(b.context_terminal_details(raw((obj + "\n") * 4)), (response, 4))
        self.assertEqual(b.context_terminal_details(raw(obj * 2, structured_output=response)), (response, 2))
        with self.assertRaisesRegex(b.BatchError, "CONTEXT_TERMINAL_RESPONSE_CONFLICT"):
            b.context_terminal_response(raw(obj + json.dumps(dict(response, transcript="other"))))
        with self.assertRaisesRegex(b.BatchError, "CONTEXT_TERMINAL_RESPONSE_CONFLICT"):
            b.context_terminal_response(raw(obj * 2, structured_output=dict(response, transcript="other")))
        for bad in (obj + " trailing", obj + "{", obj + "[]",
                    obj + '{"transcript":"alpha","transcript":"alpha","uncertainties":[],"status":"COMPLETE"}'):
            with self.assertRaisesRegex(b.BatchError, "CONTEXT_TERMINAL_RESPONSE_INVALID"):
                b.context_terminal_response(raw(bad))
        with self.assertRaisesRegex(b.BatchError, "CONTEXT_TERMINAL_TOO_MANY_DOCUMENTS"):
            b.context_terminal_response(raw(obj * 33))

    def test_context_archive_recovery_hash_pinned_idempotent_and_pending_only(self):
        original, prepare_clip = self.length_fixture()
        with patch.object(b, "prepare_lecture", side_effect=prepare_clip), patch.object(
                b, "completed_live_job", return_value={}), patch.object(
                b, "context_cli_preflight", side_effect=AssertionError("no provider")):
            prior = b.prepare_length_plan(self.root, self.root, "manifest", 60, original)
            self.complete_length_fixture(prior)
            long_plan = b.prepare_long_length_plan(self.root, self.root, "manifest", prior["length_plan"])
            self.complete_length_fixture(long_plan)
            plan = b.prepare_context_plan(self.root, long_plan["length_plan"])
            cid = plan["context_plan"]
            folder = Path("context-plans") / cid / "CURRENT_ONLY"
            prompt = b.read_bytes(b.safe_path(self.root, folder / "prompt.txt"))
            claim = b.json_bytes({"mode": "CURRENT_ONLY", "prompt_sha256": b.digest(prompt),
                                  "reservation": "POSSIBLE_SUBMISSION"})
            b.write_once(self.root, folder / "claim.json", claim)
            obj = {"transcript": "alpha alpha alpha alpha", "uncertainties": [], "status": "COMPLETE"}
            response = b.json_bytes(obj)
            stdout = b.json_bytes({"event": "result", "result": {
                "status": "SUCCESS", "response": json.dumps(obj) * 4}}).strip()
            # One physical NDJSON line, not pretty-printed outer JSON.
            stdout = json.dumps(b.parse_json(stdout)).encode()
            b.write_once(self.root, folder / "stdout.snapshot", stdout)
            b.write_once(self.root, folder / "stderr.snapshot", b"")
            args = (self.root, cid, "CURRENT_ONLY", b.digest(stdout), b.digest(b""), b.digest(response))
            with self.assertRaisesRegex(b.BatchError, "CONTEXT_RECOVERY_OUTPUT_HASH_MISMATCH"):
                b.recover_context_response(*args[:3], "0" * 64, *args[4:])
            with self.assertRaisesRegex(b.BatchError, "CONTEXT_RECOVERY_RESPONSE_HASH_MISMATCH"):
                b.recover_context_response(*args[:-1], "0" * 64)
            self.assertFalse(b.safe_path(self.root, folder / "receipt.json").exists())
            result = b.recover_context_response(*args)
            self.assertEqual(result["status"], "RECOVERED")
            self.assertEqual(result["model_requests"], 0)
            self.assertEqual(result["terminal_document_count"], 4)
            self.assertEqual(b.recover_context_response(*args)["status"], "ALREADY_CAPTURED")
            report = b.context_report(self.root, cid)
            self.assertEqual(report["pending_modes"], ["ADJACENT"])
            row = report["variants"][0]
            self.assertEqual(row["capture_source"], "HEADLESS_ARCHIVE_RECOVERY")
            self.assertIsNone(row["elapsed_seconds"])
            self.assertEqual(b.read_bytes(b.safe_path(self.root, folder / "claim.json")), claim)
            self.assertEqual(b.read_bytes(b.safe_path(self.root, folder / "stdout.snapshot")), stdout)
            b.safe_path(self.root, folder / "stderr.snapshot").write_bytes(b"tampered")
            with self.assertRaisesRegex(b.BatchError, "CONTEXT_CAPTURE_MISMATCH"):
                b.context_report(self.root, cid)

    def test_context_plan_isolates_inputs_and_blocks_unsettled_claim(self):
        original, prepare_clip = self.length_fixture()
        with patch.object(b, "prepare_lecture", side_effect=prepare_clip), patch.object(
                b, "completed_live_job", return_value={}), patch.object(
                b, "context_cli_preflight", side_effect=AssertionError("no provider")):
            prior = b.prepare_length_plan(self.root, self.root, "manifest", 60, original)
            self.complete_length_fixture(prior)
            long_plan = b.prepare_long_length_plan(self.root, self.root, "manifest", prior["length_plan"])
            self.complete_length_fixture(long_plan)
            result = b.prepare_context_plan(self.root, long_plan["length_plan"])
            cid = result["context_plan"]
            self.assertEqual(result["pending_calls"], 2)
            self.assertEqual(result["baseline"]["common_span_metrics"]["normalized_cer"]["rate"], 0)
            self.assertEqual(result, b.prepare_context_plan(self.root, long_plan["length_plan"]))
            base = Path("context-plans") / cid
            for mode in b.CONTEXT_MODES:
                prompt = b.read_bytes(b.safe_path(self.root, base / mode / "prompt.txt"))
                data = json.loads(prompt.decode().split("\nDATA_JSON:\n", 1)[1])
                self.assertEqual(set(data), {"target"} if mode == "CURRENT_ONLY"
                                 else {"previous", "target", "following"})
            with self.assertRaisesRegex(b.BatchError, "CONTEXT_AUTHORIZATION_REQUIRED"):
                b.run_context_plan(self.root, cid, 2, authorized=False)
            with self.assertRaisesRegex(b.BatchError, "CONTEXT_PENDING_EXCEEDS_APPROVED_LIMIT"):
                b.run_context_plan(self.root, cid, 1, authorized=True)
            b.write_once(self.root, base / "CURRENT_ONLY" / "claim.json", b"{}")
            with self.assertRaisesRegex(b.BatchError, "CONTEXT_UNSETTLED_CLAIM_NO_RETRY"):
                b.context_report(self.root, cid)

    def test_lecture_long_window_limit(self):
        with patch.object(b, "lecture_source_context", side_effect=b.BatchError("SOURCE_CHECK_REACHED")):
            with self.assertRaisesRegex(b.BatchError, "SOURCE_CHECK_REACHED"):
                b.prepare_lecture(self.root, self.root, "manifest", 60, 300)
            with self.assertRaisesRegex(b.BatchError, "LECTURE_WINDOW_OUT_OF_RANGE"):
                b.prepare_lecture(self.root, self.root, "manifest", 60, 301)

    def trace_audit_fixture(self, short=None):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        folder = base / job["id"]
        brain = root / "brain"
        session = brain / "new"
        session.mkdir(parents=True)
        b.claim(root, base, job)
        with patch.object(b, "fresh_brain_dirs", return_value=(brain, set())):
            b.persist_session_baseline(root, folder)
        full = '{"transcript":"alpha beta","uncertainties":[],"status":"COMPLETE"}'
        if short is None:
            short = full[:35]
        user = {"type":"USER_INPUT","source":"USER_EXPLICIT","status":"DONE",
                "content":b.PROMPT,"media":[{}]}
        hashes = {}
        for name, content in (("transcript_full.jsonl", full), ("transcript.jsonl", short)):
            trace = b"\n".join(json.dumps(row).encode() for row in (
                user, {"type":"PLANNER_RESPONSE","source":"MODEL","status":"DONE",
                       "content":content}))
            (session / name).write_bytes(trace)
            hashes[name] = b.digest(trace)
        return bid, root, base, job, brain, session, hashes

    def test_trace_audit_preserves_single_candidate_without_capture(self):
        bid, root, base, job, brain, session, hashes = self.trace_audit_fixture()
        with patch.object(b, "brain_root", return_value=brain), patch.object(
                b, "fresh_brain_dirs", return_value=(brain, {"new"})), patch.object(
                b.subprocess, "run", side_effect=AssertionError("no provider")):
            result = b.audit_job_traces(root, bid, job["id"], operator_closed=True,
                                        full_hash=hashes["transcript_full.jsonl"],
                                        short_hash=hashes["transcript.jsonl"])
        self.assertEqual(result["status"], "OPERATOR_REVIEW_REQUIRED")
        self.assertTrue(result["final_content_relation"]["short_is_exact_strict_prefix"])
        self.assertEqual(result["final_content_relation"]["common_prefix_chars"], 35)
        self.assertEqual(result["model_requests"], 0)
        self.assertNotIn("alpha beta", json.dumps(result))
        candidate = b.read_bytes(b.safe_path(root, result["candidate_path"]))
        self.assertEqual(b.validate_response(candidate)["transcript"], "alpha beta")
        self.assertEqual(b.report(root, bid)["jobs"][0]["status"], "BLOCKED_UNSETTLED_CLAIM")

    def test_trace_audit_rejects_hash_change_and_valid_conflict(self):
        other = '{"transcript":"conflict","uncertainties":[],"status":"COMPLETE"}'
        bid, root, base, job, brain, session, hashes = self.trace_audit_fixture(short=other)
        folder = base / job["id"]
        wrong = dict(hashes, **{"transcript.jsonl": "0" * 64})
        with self.assertRaisesRegex(b.BatchError, "TRACE_AUDIT_HASH_OR_PAIR_MISMATCH"):
            b.audit_trace_pair(root, folder, session, wrong)
        result = b.audit_trace_pair(root, folder, session, hashes)
        self.assertEqual(result["trace_diagnostic"]["status"], "TRACE_FINAL_RESPONSE_CONFLICT")
        self.assertNotIn("candidate_path", result)

    def test_wait_live_single_valid_stable_stops_with_review_not_capture(self):
        from unittest.mock import Mock
        bid, root, base, job, brain, session, hashes = self.trace_audit_fixture()
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        review = Mock(side_effect=lambda path, expected: b.audit_trace_pair(
            root, base / job["id"], path, expected))
        with patch.object(b, "session_from_baseline", return_value=session), patch.object(
                b.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                b.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(b.BatchError, "LIVE_SINGLE_TRACE_REVIEW_REQUIRED_NO_RETRY"):
                b.wait_live_pair((brain, set()), timeout=10, review=review, review_settle=3)
        review.assert_called_once_with(session, hashes)
        self.assertEqual(b.report(root, bid)["jobs"][0]["status"], "BLOCKED_UNSETTLED_CLAIM")

    def full_primary_fixture(self):
        full_content = json.dumps({"transcript": "alpha " * 100,
                                   "uncertainties": [], "status": "COMPLETE"})
        short_content = full_content[:250] + "\n" + full_content[450:]
        user = {"type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE",
                "content": b.PROMPT, "media": [{}]}
        final = {"type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE"}
        def encode(content, input_row=None, final_row=None):
            return b"\n".join(json.dumps(row).encode() for row in (
                user if input_row is None else input_row,
                dict(final if final_row is None else final_row, content=content)))
        return encode(full_content), encode(short_content), encode, user, final

    def test_full_primary_requires_same_envelope_and_invalid_short(self):
        full, short, encode, user, final = self.full_primary_fixture()
        candidate = b.full_primary_candidate(full, short)
        self.assertIsNotNone(candidate)
        self.assertEqual(b.validate_response(candidate)["transcript"], "alpha " * 100)
        conflict = json.dumps({"transcript": "other", "uncertainties": [], "status": "COMPLETE"})
        self.assertIsNone(b.full_primary_candidate(full, encode(conflict)))
        short_body, _ = b.final_content_shape(short)
        self.assertIsNone(b.full_primary_candidate(full, encode(short_body, dict(user, media=[{"other": True}]))))
        self.assertIsNone(b.full_primary_candidate(full, encode(short_body, final_row=dict(final, tool_calls=[{}]))))
        self.assertIsNone(b.full_primary_candidate(full, encode(short_body, final_row=dict(final, status="RUNNING"))))
        self.assertIsNone(b.full_primary_candidate(full, encode(short_body[:200])))
        self.assertIsNone(b.full_primary_candidate(short, full))

    def test_full_primary_wait_opt_in_preserves_both_records(self):
        bid, root, base, job, brain, session, hashes = self.trace_audit_fixture()
        full, short, _, _, _ = self.full_primary_fixture()
        for name, raw in (("transcript_full.jsonl", full), ("transcript.jsonl", short)):
            (session / name).write_bytes(raw)
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(b, "session_from_baseline", return_value=session), patch.object(
                b.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                b.time, "sleep", side_effect=sleep):
            pair = b.wait_live_pair((brain, set()), timeout=10, review_settle=3,
                                    review=lambda path, expected: b.audit_trace_pair(
                                        root, base / job["id"], path, expected),
                                    allow_full_primary=True)
        self.assertEqual(pair, (full, b.full_primary_candidate(full, short), short))
        self.assertEqual(b.report(root, bid)["jobs"][0]["status"], "BLOCKED_UNSETTLED_CLAIM")

    def test_full_primary_capture_reuse_checks_malformed_alias(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        folder = base / job["id"]
        full, short, _, _, _ = self.full_primary_fixture()
        candidate = b.full_primary_candidate(full, short)
        b.claim(root, base, job)
        b.write_once(root, folder / "live-session.snapshot", full)
        b.write_once(root, folder / "live-session-alias.snapshot", short)
        b.write_once(root, folder / "live-pilot.json", b.json_bytes({
            "capture_source": b.FULL_PRIMARY_SOURCE,
            "completion_evidence": "FULL_PRIMARY_MALFORMED_ALIAS_STABLE_30S",
            "full_primary_authorized": True, "independent_alias_agreement": False,
            "session_sha256": b.digest(full), "alias_sha256": b.digest(short),
            "window_close_status": "CLOSED"}))
        b.capture(root, base, job, candidate, 35, source=b.FULL_PRIMARY_SOURCE)
        row = b.report(root, bid)["jobs"][0]
        self.assertEqual(row["capture_source"], b.FULL_PRIMARY_SOURCE)
        self.assertFalse(b.completed_live_job(root, base, job, row)["independent_alias_agreement"])
        b.safe_path(root, folder / "live-session-alias.snapshot").write_bytes(b"tampered")
        with self.assertRaisesRegex(b.BatchError, "FULL_PRIMARY_EVIDENCE_MISMATCH"):
            b.completed_live_job(root, base, job, row)

    def test_stage_prompt_retains_clipboard_and_pastes_once(self):
        from unittest.mock import Mock
        pg, clip, target = Mock(), Mock(), Mock()
        clip.paste.return_value = b.PROMPT.replace("\n", "\r\n")
        with patch.object(b.time, "sleep"):
            b.stage_prompt(pg, clip, target)
        clip.copy.assert_called_once_with(b.PROMPT)
        pg.hotkey.assert_called_once_with("ctrl", "v", interval=.1)
        self.assertEqual(target.call_count, 2)
        pg.reset_mock()
        clip.paste.return_value = "wrong"
        with self.assertRaisesRegex(b.BatchError, "PROMPT_CLIPBOARD_VERIFY_FAILED"):
            b.stage_prompt(pg, clip, target)
        pg.hotkey.assert_not_called()

    def test_stage_prompt_retries_transient_clipboard_copy_and_read_only(self):
        from unittest.mock import Mock
        class ClipboardBusy(Exception):
            pass
        pg, clip, target = Mock(), Mock(), Mock()
        clip.PyperclipWindowsException = ClipboardBusy
        clip.copy.side_effect = [ClipboardBusy("private detail"), None, None]
        clip.paste.side_effect = [ClipboardBusy("private detail"), b.PROMPT]
        with patch.object(b.time, "sleep") as sleep:
            b.stage_prompt(pg, clip, target)
        self.assertEqual(clip.copy.call_count, 3)
        self.assertEqual(clip.paste.call_count, 2)
        pg.hotkey.assert_called_once_with("ctrl", "v", interval=.1)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [.5, 1, 1])

    def test_stage_prompt_persistent_clipboard_failure_has_no_paste(self):
        from unittest.mock import Mock
        class ClipboardBusy(Exception):
            pass
        pg, clip, target = Mock(), Mock(), Mock()
        clip.PyperclipWindowsException = ClipboardBusy
        clip.copy.side_effect = ClipboardBusy("private detail")
        with patch.object(b.time, "sleep") as sleep:
            with self.assertRaisesRegex(b.BatchError, "^PROMPT_CLIPBOARD_ACCESS_FAILED$"):
                b.stage_prompt(pg, clip, target)
        self.assertEqual(clip.copy.call_count, 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [.5, 1, 2, 3])
        clip.paste.assert_not_called()
        pg.hotkey.assert_not_called()

    def test_stage_prompt_does_not_retry_focus_or_paste_errors(self):
        from unittest.mock import Mock
        pg, clip, target = Mock(), Mock(), Mock()
        clip.paste.return_value = b.PROMPT
        target.side_effect = [None, OSError("focus failed")]
        with patch.object(b.time, "sleep"):
            with self.assertRaisesRegex(OSError, "focus failed"):
                b.stage_prompt(pg, clip, target)
        self.assertEqual(clip.copy.call_count, 1)
        pg.hotkey.assert_not_called()
        target.side_effect = None
        pg.hotkey.side_effect = OSError("paste failed")
        with patch.object(b.time, "sleep"):
            with self.assertRaisesRegex(OSError, "paste failed"):
                b.stage_prompt(pg, clip, target)
        self.assertEqual(clip.copy.call_count, 2)
        pg.hotkey.assert_called_once_with("ctrl", "v", interval=.1)

    def test_prompt_recovery_preserves_claim_and_records_distinct_reason(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        folder = base / job["id"]
        brain = root / "brain"
        brain.mkdir()
        b.claim(root, base, job)
        b.write_once(root, folder / "AGY_WP1_0123456789AB.ps1", b"synthetic")
        with patch.object(b, "fresh_brain_dirs", return_value=(brain, set())):
            b.persist_session_baseline(root, folder)
        old = b.read_bytes(b.safe_path(root, folder / "claim.json"))
        with patch("tools.agy_media_probe.windows", return_value=[]), patch.object(
                b, "fresh_brain_dirs", return_value=(brain, set())):
            b.authorize_clipboard_recovery(root, base, plan, job, None,
                                           reason="PROMPT_NOT_INSERTED")
            with self.assertRaisesRegex(b.BatchError, "CLIPBOARD_RECOVERY_ALREADY_USED"):
                b.authorize_clipboard_recovery(root, base, plan, job, None)
        receipt = b.parse_json(b.read_bytes(b.safe_path(root, folder / "clipboard-recovery.json")))
        self.assertTrue(receipt["operator_declared_prompt_not_inserted_before_submit"])
        self.assertFalse(receipt["operator_declared_clipboard_failure_before_submit"])
        self.assertFalse(receipt["provider_no_submission_verified"])
        self.assertEqual(b.read_bytes(b.safe_path(root, folder / "claim.json")), old)

    def test_live_batch_prompt_recovery_routes_single_existing_claim(self):
        bid = self.prepare()
        root, base, plan = b.load_plan(self.root, bid)
        job = plan["jobs"][0]
        b.claim(root, base, job)
        def fake_job(root, base, plan, job, pg, clip, **kwargs):
            self.assertEqual(kwargs, {"recover_clipboard": True, "recovery_reason": "PROMPT_NOT_INSERTED"})
            b.capture(root, base, job, b.json_bytes({
                "transcript":"alpha", "uncertainties":[], "status":"COMPLETE"}), 1,
                source="SESSION_TRACE_PILOT")
            return {"window_close_status":"CLOSED"}
        with patch.object(b,"is_windows",return_value=True), patch.object(
                b,"validate_live_proofs",return_value={}), patch.object(
                b,"capability_probe",return_value={"version":"1.5.16"}), patch.object(
                b,"gui_context",return_value=(None,None)), patch.object(b,"live_job",side_effect=fake_job) as run:
            result=b.live_batch(root,bid,"paired","pilot",1,recover_prompt_job=job["id"])
            self.assertEqual(run.call_count,1)
            self.assertEqual(result["reserved_attempts"],1)
            self.assertEqual(result["live_batch"]["recovered_prompt_job"],job["id"])
            self.assertIsNone(result["live_batch"]["recovered_clipboard_job"])

    def test_cli_run_requires_authorization(self):
        with patch.object(b, "guided_run", side_effect=AssertionError("live forbidden")):
            self.assertEqual(b.main(["run", "--root", str(self.root),
                                     "--batch", "invalid", "--max-calls", "1"]), 2)


if __name__ == "__main__":
    unittest.main()
