import contextlib
import io
import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from ytresearch.collector import video_id_from_url, choose_transcript
from ytresearch.pipeline import import_csv, split_segments, extract_one, analyze_one, audit_one, validate_claims
from ytresearch.storage import atomic_write, digest, json_text, connect, video_dir, read_json


class FakeRunner:
    def run(self, role, prompt, schema):
        if role == "extractor":
            return {"claims": [{"section": 13, "text": "명령어 demo --safe 실행", "segment_ids": ["s000001"], "kind": "demonstrated"}]}, {}
        if role == "analyzer":
            # Parse the required headings from the prompt, not from the test assumptions.
            import re
            numbers = [int(x) for x in re.findall(r"(?m)^(\d+)\. ", prompt)]
            return {"sections": [{"number": n, "markdown": "근거 문장 [s000001]" if n == 13 else "원본에서 직접 확인되지 않음"} for n in numbers]}, {}
        if role == "auditor":
            return {"issues": []}, {}
        raise AssertionError(role)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "agents.json").write_text(json.dumps({"max_chars_per_chunk": 5500}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_url_and_duplicate_alias(self):
        self.assertEqual(video_id_from_url("https://www.youtube.com/watch?v=Tst00000054"), "Tst00000054")
        self.assertEqual(video_id_from_url("https://youtu.be/Tst00000054"), "Tst00000054")
        file = self.root / "sources.csv"
        file.write_text("source_id,url\nS019,https://youtu.be/Tst00000054\nS024,https://youtu.be/Tst00000054\n", encoding="utf-8")
        self.assertEqual(import_csv(self.root, file), 2)
        with closing(connect(self.root)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM videos").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM entries").fetchone()[0], 2)

    def test_no_raw_overwrite(self):
        path = self.root / "raw.txt"
        atomic_write(path, "alpha", immutable=True)
        with self.assertRaises(RuntimeError):
            atomic_write(path, "beta", immutable=True)

    def test_bad_section_index_is_reported_without_claim_content(self):
        private = "DO_NOT_LOG_PRIVATE_CLAIM_TEXT"
        chunk = [{"id": "s000001", "start": 0.0, "text": "source"}]
        for section, expected in ((19, "safe_value=19"), ("19", "type=str"),
                                  ("private " + private, "safe_value=<redacted>"),
                                  (True, "type=bool")):
            with self.subTest(section=type(section).__name__):
                with self.assertRaisesRegex(RuntimeError, "CLAIM_SECTION_INVALID") as ctx:
                    validate_claims([{
                        "section": section, "text": private,
                        "segment_ids": ["s000001"], "kind": "source_claim",
                    }], chunk)
                self.assertIn("claims[1].section", str(ctx.exception))
                self.assertIn(expected, str(ctx.exception))
                self.assertNotIn(private, str(ctx.exception))

    def test_chunk_lossless(self):
        segments = [{"id": f"s{i:06d}", "start": i, "duration": 1, "text": "x" * 40} for i in range(1, 11)]
        split = split_segments(segments, 130)
        self.assertEqual([s for group in split for s in group], segments)

    def test_extract_progress_four_chunks_and_cache(self):
        import re

        class ProgressRunner:
            def run(self, role, prompt, schema):
                evidence = re.search(r"\[(s\d{6}) @ ", prompt)
                self_id = evidence.group(1)
                return {
                    "claims": [{
                        "section": 1, "text": "한 세그먼트 근거",
                        "segment_ids": [self_id], "kind": "source_claim",
                    }]
                }, {}

        (self.root / "config" / "agents.json").write_text(
            json.dumps({"max_chars_per_chunk": 50}), encoding="utf-8"
        )
        vid = "Tst00000054"
        path = video_dir(self.root, vid)
        segments = [
            {"id": f"s{i:06d}", "start": float(i), "duration": 1.0,
             "text": "짧은 실제 원문 한 문장"}
            for i in range(1, 5)
        ]
        original = json_text({"video_id": vid, "segments": segments})
        atomic_write(path / "transcript.json", original)
        atomic_write(path / "transcript.sha256", digest(original) + "\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(extract_one(self.root, vid, ProgressRunner()), (4, 4))
        progress = out.getvalue()
        for mark in ("0%", "25%", "50%", "75%", "100%"):
            self.assertIn(mark, progress)
        self.assertTrue((path / "extraction.json").exists())
        with contextlib.redirect_stdout(io.StringIO()) as again:
            self.assertEqual(extract_one(self.root, vid, ProgressRunner()), (4, 4))
        self.assertEqual(again.getvalue().count("기존 캐시 재사용"), 4)

    def test_end_to_end_mock(self):
        vid = "Tst00000054"
        path = video_dir(self.root, vid)
        payload = {"video_id": vid, "language_code": "ko", "segments": [{"id": "s000001", "start": 0, "duration": 3, "text": "명령어 demo --safe 실행"}]}
        raw = json_text(payload)
        atomic_write(path / "transcript.json", raw)
        atomic_write(path / "transcript.sha256", digest(raw) + "\n")
        with closing(connect(self.root)) as db:
            db.execute("INSERT INTO videos(video_id,url) VALUES(?,?)", (vid, "https://youtube.com/watch?v=" + vid))
            db.commit()
        self.assertEqual(extract_one(self.root, vid, FakeRunner()), (1, 1))
        self.assertEqual(analyze_one(self.root, vid, FakeRunner()), 19)
        state, count = audit_one(self.root, vid, FakeRunner())
        self.assertEqual(state, "MODEL_PASS_REVIEW_REQUIRED")
        self.assertEqual(count, 0)
        self.assertEqual((path / "analysis.md").read_text(encoding="utf-8").count("\n## "), 19)
        self.assertFalse(read_json(path / "audit.json")["human_approved"])


if __name__ == "__main__":
    unittest.main()

class VttTests(unittest.TestCase):
    def test_vtt_import(self):
        from ytresearch.collector import ingest_vtt
        from ytresearch.storage import read_json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vtt = root / "test.vtt"
            vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n안녕하세요 <b>세계</b>\n\n00:00:03.000 --> 00:00:04.500\n테스트\n", encoding="utf-8")
            _, length = ingest_vtt(root, "Tst00000054", vtt, "ko")
            self.assertEqual(length, 2)
            transcript = read_json(root / "data" / "videos" / "Tst00000054" / "transcript.json")
            self.assertEqual(transcript["segments"][0]["text"], "안녕하세요 세계")
            self.assertEqual(transcript["segments"][1]["start"], 3.0)
