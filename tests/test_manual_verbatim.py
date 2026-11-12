"""Verbatim source preservation and explicit manual-review contract."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ytresearch.manual_verbatim import (
    VerbatimError, prepare, import_review, export, render_captions, inputs
)


class ManualVerbatimTests(unittest.TestCase):
    def make_files(self, d):
        root = Path(d)
        (root / "data").mkdir()
        (root / "original").mkdir()
        words = "Hello    world\n!"
        backticks = chr(96) * 3
        captions = {"events": [
            {"tStartMs": 100, "dDurationMs": 900,
             "segs": [{"utf8": words}]},
            {"tStartMs": 1100, "dDurationMs": 0, "segs": []},
            {"tStartMs": 1500, "dDurationMs": 500,
             "segs": [{"utf8": backticks + "no strip" + backticks}]}
        ]}
        assets = {}
        for role in ("primary_video", "highres_video", "audio", "subtitle_json3"):
            p = root / "original" / (role + ".dat")
            raw = (json.dumps(captions).encode() if role == "subtitle_json3"
                   else role.encode())
            p.write_bytes(raw)
            assets[role] = {"path": str(p), "size_bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()}
        mf = root / "data" / "pilot.json"
        mf.write_text(json.dumps({"video_id": "Tst00000001", "assets": assets}),
                      encoding="utf-8")
        return root, mf, assets

    def test_prepare_and_import_export_preserve_all_text(self):
        with tempfile.TemporaryDirectory() as d:
            root, mf, assets = self.make_files(d)
            with patch("ytresearch.manual_verbatim.read_source_ledger",
                       return_value={"videos": [{"video_id": "Tst00000001"}]}):
                info = prepare(root, mf)
                self.assertEqual(info["caption_events"], 2)
                self.assertFalse(info["audio_verified"])
                dest = root / "data" / "verbatim" / "Tst00000001"
                self.assertEqual((dest / "original_captions.json3").read_bytes(),
                                 Path(assets["subtitle_json3"]["path"]).read_bytes())
                text = (dest / "captions_unedited.md").read_text(encoding="utf-8")
                self.assertIn("Hello    world\n!", text)
                self.assertIn(chr(96) * 3 + "no strip", text)
                self.assertEqual(prepare(root, mf)["status"], "VERBATIM_PREPARED")
                a, b = root / "flash.txt", root / "argon.txt"
                a.write_text("Flash words", encoding="utf-8")
                b.write_text("Argon words", encoding="utf-8")
                import_review(root, mf, "flash", a)
                import_review(root, mf, "argon", b)
                output = export(root, mf)
                self.assertFalse(output["human_approved"])
                combined = (dest / "verbatim_timeline_REVIEW_REQUIRED.md").read_text(encoding="utf-8")
                self.assertIn("Flash words", combined)
                self.assertIn("Argon words", combined)
                self.assertIn("Hello    world\n!", combined)

    def test_changed_source_and_modified_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root, mf, assets = self.make_files(d)
            with patch("ytresearch.manual_verbatim.read_source_ledger",
                       return_value={"videos": [{"video_id": "Tst00000001"}]}):
                prepare(root, mf)
                (root / "data" / "verbatim" / "Tst00000001" / "captions_unedited.md").write_text("tampered")
                with self.assertRaisesRegex(VerbatimError, "OUTPUT_CONFLICT"):
                    prepare(root, mf)
                Path(assets["audio"]["path"]).write_text("changed")
                with self.assertRaisesRegex(VerbatimError, "ASSET_"):
                    inputs(root, mf)

    def test_no_audio_proof_from_json3(self):
        text, events, used = render_captions(json.dumps({
            "events": [{"tStartMs": 0, "segs": [{"utf8": "word word"}]}]}).encode())
        self.assertEqual((events, used), (1, 1))
        self.assertIn("not verified against audio", text)
        self.assertNotIn("19-section", text)


if __name__ == "__main__":
    unittest.main()
