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

    def test_cli_run_requires_authorization(self):
        with patch.object(b, "guided_run", side_effect=AssertionError("live forbidden")):
            self.assertEqual(b.main(["run", "--root", str(self.root),
                                     "--batch", "invalid", "--max-calls", "1"]), 2)


if __name__ == "__main__":
    unittest.main()
