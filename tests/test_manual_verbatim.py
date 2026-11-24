"""Verbatim source preservation and explicit manual-review contract."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
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

    def test_remux_streams_use_opaque_codec_copy_for_decoder_only_av1(self):
        from ytresearch.manual_verbatim import _create_remux_streams

        class FakeOutput:
            def __init__(self):
                self.calls = []

            def add_stream_from_template(self, stream, *, opaque=False):
                self.calls.append((stream, opaque))
                if not opaque:
                    raise RuntimeError("UnknownCodecError: libdav1d")
                return "copied-" + stream

        output = FakeOutput()
        self.assertEqual(
            _create_remux_streams(output, ("libdav1d-av1", "aac")),
            ["copied-libdav1d-av1", "copied-aac"],
        )
        self.assertEqual(
            output.calls, [("libdav1d-av1", True), ("aac", True)]
        )


    def test_ffmpeg_command_h264_video_aac_stream_copy(self):
        from ytresearch.manual_verbatim import _compatible_ffmpeg_command

        cmd = _compatible_ffmpeg_command(
            "ffmpeg.exe", "video.mp4", "audio.m4a", "output.partial.mp4",
        )
        self.assertEqual(cmd[0], "ffmpeg.exe")
        self.assertEqual([cmd[i+1] for i, x in enumerate(cmd) if x == "-map"],
                         ["0:v:0", "1:a:0"])
        self.assertEqual(cmd[cmd.index("-c:v")+1], "libx264")
        self.assertEqual(cmd[cmd.index("-c:a")+1], "copy")
        self.assertEqual(cmd[cmd.index("-pix_fmt")+1], "yuv420p")
        self.assertIn("-n", cmd)
        self.assertEqual(cmd[-1], "output.partial.mp4")

    def test_missing_ffmpeg_does_not_modify_source(self):
        from ytresearch.manual_verbatim import playback_compatible

        with tempfile.TemporaryDirectory() as d:
            root, manifest, assets = self.make_files(d)
            before = {k: v["sha256"] for k, v in assets.items()}
            with (patch("ytresearch.manual_verbatim.read_source_ledger",
                        return_value={"videos": [{"video_id": "Tst00000001"}]}),
                  patch("ytresearch.manual_verbatim.shutil.which", return_value=None)):
                with self.assertRaisesRegex(VerbatimError, "FFMPEG_MISSING"):
                    playback_compatible(root, manifest)
            self.assertEqual(
                {k: hashlib.sha256(Path(v["path"]).read_bytes()).hexdigest()
                 for k, v in assets.items()}, before,
            )

    def test_no_audio_proof_from_json3(self):
        text, events, used = render_captions(json.dumps({
            "events": [{"tStartMs": 0, "segs": [{"utf8": "word word"}]}]}).encode())
        self.assertEqual((events, used), (1, 1))
        self.assertIn("not verified against audio", text)
        self.assertNotIn("19-section", text)

    def test_verbatim_single_v1_unregistered_video_without_subtitles(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000099"  # Synthetic ID not in 77-source ledger
            raw_video = b"simulated_video_stream_bytes"
            raw_audio = b"simulated_audio_stream_bytes"
            vp = root / "original" / "vid.mp4"
            ap = root / "original" / "aud.m4a"
            vp.write_bytes(raw_video)
            ap.write_bytes(raw_audio)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_video),
                        "sha256": hashlib.sha256(raw_video).hexdigest(),
                    },
                    "audio": {
                        "path": str(ap),
                        "size_bytes": len(raw_audio),
                        "sha256": hashlib.sha256(raw_audio).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "single_manifest.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")

            # Must succeed without touching read_source_ledger
            found, out = inputs(root, mf)
            self.assertEqual(found["primary_video"], vp.resolve())
            self.assertEqual(found["audio"], ap.resolve())
            self.assertEqual(found["highres_video"], vp.resolve())  # fallback
            self.assertNotIn("subtitle_json3", found)

            info = prepare(root, mf)
            self.assertEqual(info["status"], "VERBATIM_PREPARED")
            self.assertEqual(info["caption_events"], 0)
            self.assertFalse(info["caption_available"])
            self.assertFalse((out / "original_captions.json3").exists())
            self.assertFalse((out / "captions_unedited.md").exists())
            self.assertTrue((out / "FLASH_INSTRUCTIONS.txt").exists())
            self.assertTrue((out / "ARGON_INSTRUCTIONS.txt").exists())

            flash_rev = root / "flash.txt"
            argon_rev = root / "argon.txt"
            flash_rev.write_text("Flash transcript draft without captions", encoding="utf-8")
            argon_rev.write_text("Argon corrected draft without captions", encoding="utf-8")
            import_review(root, mf, "flash", flash_rev)
            import_review(root, mf, "argon", argon_rev)

            res = export(root, mf)
            self.assertEqual(res["status"], "VERBATIM_REVIEW_REQUIRED")
            timeline = (out / "verbatim_timeline_REVIEW_REQUIRED.md").read_text(encoding="utf-8")
            self.assertIn("Flash transcript draft without captions", timeline)
            self.assertIn("Argon corrected draft without captions", timeline)
            self.assertIn("자막 미제공: 영상 음성 직접 전사 대상", timeline)

    def test_verbatim_single_v1_embedded_audio_and_ffmpeg_mapping(self):
        from ytresearch.manual_verbatim import _compatible_ffmpeg_command

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000088"
            raw_video = b"video_with_embedded_audio"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_video)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_video),
                        "sha256": hashlib.sha256(raw_video).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "single_embedded.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")

            found, _ = inputs(root, mf)
            self.assertEqual(found["primary_video"], vp.resolve())
            self.assertNotIn("audio", found)

            cmd = _compatible_ffmpeg_command(
                "ffmpeg.exe", str(vp), None, "out.mp4",
            )
            self.assertEqual(cmd[0], "ffmpeg.exe")
            # When audio is None, ffmpeg maps video and audio from input 0
            self.assertEqual([cmd[i+1] for i, x in enumerate(cmd) if x == "-map"],
                             ["0:v:0", "0:a:0"])
            self.assertEqual(cmd[cmd.index("-c:a")+1], "copy")

    def test_verbatim_single_v1_validation_rejects_malformed_assets(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(b"data")
            v_sha = hashlib.sha256(b"data").hexdigest()

            # Disallowed role
            mf = root / "data" / "bad_role.json"
            mf.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000077",
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": 4, "sha256": v_sha},
                    "unknown_role": {"path": str(vp), "size_bytes": 4, "sha256": v_sha},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "ASSET_ROLES_INVALID"):
                inputs(root, mf)

            # Missing primary_video
            mf_no_pv = root / "data" / "no_pv.json"
            mf_no_pv.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000077",
                "assets": {
                    "audio": {"path": str(vp), "size_bytes": 4, "sha256": v_sha},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "ASSET_ROLES_INVALID"):
                inputs(root, mf_no_pv)

            # Hash mismatch
            mf_bad_hash = root / "data" / "bad_hash.json"
            mf_bad_hash.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000077",
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": 4, "sha256": "0" * 64},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "ASSET_HASH_MISMATCH"):
                inputs(root, mf_bad_hash)

            # Size mismatch
            mf_bad_size = root / "data" / "bad_size.json"
            mf_bad_size.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000077",
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": 999, "sha256": v_sha},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "ASSET_SIZE_MISMATCH"):
                inputs(root, mf_bad_size)

            # Symlink asset rejected
            link = root / "original" / "link.mp4"
            try:
                link.symlink_to(vp)
                mf_link = root / "data" / "link.json"
                mf_link.write_text(json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000077",
                    "assets": {
                        "primary_video": {"path": str(link), "size_bytes": 4, "sha256": v_sha},
                    },
                }), encoding="utf-8")
                with self.assertRaisesRegex(VerbatimError, "ASSET_UNAVAILABLE"):
                    inputs(root, mf_link)
            except OSError:
                pass  # Symlink privilege may not be available on some Windows accounts

    def test_legacy_manifest_still_requires_registry_and_all_roles(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(b"data")
            v_sha = hashlib.sha256(b"data").hexdigest()

            # Legacy manifest without schema_version must check read_source_ledger
            mf = root / "data" / "legacy.json"
            mf.write_text(json.dumps({
                "video_id": "Tst00000099",
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": 4, "sha256": v_sha},
                },
            }), encoding="utf-8")
            with patch("ytresearch.manual_verbatim.read_source_ledger",
                       return_value={"videos": [{"video_id": "Tst00000001"}]}):
                with self.assertRaisesRegex(VerbatimError, "VIDEO_NOT_REGISTERED"):
                    inputs(root, mf)

            with patch("ytresearch.manual_verbatim.read_source_ledger",
                       return_value={"videos": [{"video_id": "Tst00000099"}]}):
                # Missing roles for legacy format
                with self.assertRaisesRegex(VerbatimError, "ASSET_ROLES_INVALID"):
                    inputs(root, mf)

    def test_verbatim_single_v1_with_json3_captions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000089"
            captions = {"events": [
                {"tStartMs": 200, "dDurationMs": 800, "segs": [{"utf8": "Single video line"}]}
            ]}
            raw_j3 = json.dumps(captions).encode("utf-8")
            raw_v = b"single_video_content"
            vp = root / "original" / "vid.mp4"
            jp = root / "original" / "sub.json3"
            vp.write_bytes(raw_v)
            jp.write_bytes(raw_j3)

            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_v),
                        "sha256": hashlib.sha256(raw_v).hexdigest(),
                    },
                    "subtitle_json3": {
                        "path": str(jp),
                        "size_bytes": len(raw_j3),
                        "sha256": hashlib.sha256(raw_j3).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "single_with_captions.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")

            found, out = inputs(root, mf)
            self.assertEqual(found["primary_video"], vp.resolve())
            self.assertEqual(found["subtitle_json3"], jp.resolve())

            info = prepare(root, mf)
            self.assertEqual(info["status"], "VERBATIM_PREPARED")
            self.assertEqual(info["caption_events"], 1)
            self.assertTrue(info["caption_available"])
            self.assertTrue((out / "original_captions.json3").is_file())
            self.assertTrue((out / "captions_unedited.md").is_file())

            f_txt, a_txt = root / "f.txt", root / "a.txt"
            f_txt.write_text("Flash transcript", encoding="utf-8")
            a_txt.write_text("Argon transcript", encoding="utf-8")
            import_review(root, mf, "flash", f_txt)
            import_review(root, mf, "argon", a_txt)

            out_info = export(root, mf)
            self.assertEqual(out_info["status"], "VERBATIM_REVIEW_REQUIRED")
            timeline = (out / "verbatim_timeline_REVIEW_REQUIRED.md").read_text(encoding="utf-8")
            self.assertIn("Single video line", timeline)
            self.assertIn("Flash transcript", timeline)
            self.assertIn("Argon transcript", timeline)

    def test_verbatim_single_v1_export_requires_both_drafts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000087"
            raw_v = b"video_content"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_v)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_v),
                        "sha256": hashlib.sha256(raw_v).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "mf.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            prepare(root, mf)

            # Neither draft imported
            with self.assertRaisesRegex(VerbatimError, "SOURCE_AND_BOTH_MODEL_DRAFTS_REQUIRED"):
                export(root, mf)

            f_txt = root / "flash.txt"
            f_txt.write_text("Flash only", encoding="utf-8")
            import_review(root, mf, "flash", f_txt)

            # Only flash imported
            with self.assertRaisesRegex(VerbatimError, "SOURCE_AND_BOTH_MODEL_DRAFTS_REQUIRED"):
                export(root, mf)

    def test_verbatim_single_v1_cli_invocations(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000086"
            raw_v = b"video_cli_test"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_v)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_v),
                        "sha256": hashlib.sha256(raw_v).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "cli_mf.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")

            # Test prepare CLI
            cmd_prep = [
                sys.executable, "-m", "ytresearch", "--root", str(root),
                "verbatim-manual", "prepare", "--manifest", str(mf),
            ]
            res_prep = subprocess.run(cmd_prep, capture_output=True, text=True, check=True)
            self.assertIn('"status": "VERBATIM_PREPARED"', res_prep.stdout)

            # Test import CLI
            f_txt, a_txt = root / "f.txt", root / "a.txt"
            f_txt.write_text("Flash CLI", encoding="utf-8")
            a_txt.write_text("Argon CLI", encoding="utf-8")

            cmd_f = [
                sys.executable, "-m", "ytresearch", "--root", str(root),
                "verbatim-manual", "import", "--manifest", str(mf),
                "--stage", "flash", "--file", str(f_txt),
            ]
            subprocess.run(cmd_f, capture_output=True, text=True, check=True)

            cmd_a = [
                sys.executable, "-m", "ytresearch", "--root", str(root),
                "verbatim-manual", "import", "--manifest", str(mf),
                "--stage", "argon", "--file", str(a_txt),
            ]
            subprocess.run(cmd_a, capture_output=True, text=True, check=True)

            # Test export CLI
            cmd_exp = [
                sys.executable, "-m", "ytresearch", "--root", str(root),
                "verbatim-manual", "export", "--manifest", str(mf),
            ]
            res_exp = subprocess.run(cmd_exp, capture_output=True, text=True, check=True)
            self.assertIn('"status": "VERBATIM_REVIEW_REQUIRED"', res_exp.stdout)

    def test_f01_legacy_manifest_missing_caption_archive_fails_export(self):
        with tempfile.TemporaryDirectory() as d:
            root, mf, _ = self.make_files(d)
            with patch("ytresearch.manual_verbatim.read_source_ledger",
                       return_value={"videos": [{"video_id": "Tst00000001"}]}):
                prepare(root, mf)
                a, b = root / "flash.txt", root / "argon.txt"
                a.write_text("Flash words", encoding="utf-8")
                b.write_text("Argon words", encoding="utf-8")
                import_review(root, mf, "flash", a)
                import_review(root, mf, "argon", b)
                cap_file = root / "data" / "verbatim" / "Tst00000001" / "original_captions.json3"
                cap_file.unlink()
                with self.assertRaisesRegex(VerbatimError, "SOURCE_AND_BOTH_MODEL_DRAFTS_REQUIRED"):
                    export(root, mf)

    def test_f01_single_v1_with_captions_missing_archive_fails_export(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000089"
            captions = {"events": [
                {"tStartMs": 200, "dDurationMs": 800, "segs": [{"utf8": "Single video line"}]}
            ]}
            raw_j3 = json.dumps(captions).encode("utf-8")
            raw_v = b"single_video_content"
            vp = root / "original" / "vid.mp4"
            jp = root / "original" / "sub.json3"
            vp.write_bytes(raw_v)
            jp.write_bytes(raw_j3)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_v),
                        "sha256": hashlib.sha256(raw_v).hexdigest(),
                    },
                    "subtitle_json3": {
                        "path": str(jp),
                        "size_bytes": len(raw_j3),
                        "sha256": hashlib.sha256(raw_j3).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "single_with_captions.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            prepare(root, mf)
            f_txt, a_txt = root / "f.txt", root / "a.txt"
            f_txt.write_text("Flash transcript", encoding="utf-8")
            a_txt.write_text("Argon transcript", encoding="utf-8")
            import_review(root, mf, "flash", f_txt)
            import_review(root, mf, "argon", a_txt)
            _, out = inputs(root, mf)
            (out / "captions_unedited.md").unlink()
            with self.assertRaisesRegex(VerbatimError, "SOURCE_AND_BOTH_MODEL_DRAFTS_REQUIRED"):
                export(root, mf)

    def test_f01_single_v1_without_captions_normal_export(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000099"
            raw_v = b"simulated_video"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_v)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_v),
                        "sha256": hashlib.sha256(raw_v).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "single_no_sub.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            prepare(root, mf)
            f_txt, a_txt = root / "f.txt", root / "a.txt"
            f_txt.write_text("Flash draft", encoding="utf-8")
            a_txt.write_text("Argon draft", encoding="utf-8")
            import_review(root, mf, "flash", f_txt)
            import_review(root, mf, "argon", a_txt)
            res = export(root, mf)
            self.assertEqual(res["status"], "VERBATIM_REVIEW_REQUIRED")
            _, out = inputs(root, mf)
            text = (out / "verbatim_timeline_REVIEW_REQUIRED.md").read_text(encoding="utf-8")
            self.assertIn("자막 미제공: 영상 음성 직접 전사 대상", text)

    def test_f04_tampered_json3_and_markdown_both_modified_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root, mf, _ = self.make_files(d)
            with patch("ytresearch.manual_verbatim.read_source_ledger",
                       return_value={"videos": [{"video_id": "Tst00000001"}]}):
                prepare(root, mf)
                f_txt, a_txt = root / "f.txt", root / "a.txt"
                f_txt.write_text("Flash draft", encoding="utf-8")
                a_txt.write_text("Argon draft", encoding="utf-8")
                import_review(root, mf, "flash", f_txt)
                import_review(root, mf, "argon", a_txt)
                _, out = inputs(root, mf)
                forged_captions = {"events": [
                    {"tStartMs": 500, "dDurationMs": 1000, "segs": [{"utf8": "FORGED CAPTIONS"}]}
                ]}
                forged_raw = json.dumps(forged_captions).encode("utf-8")
                forged_md, _, _ = render_captions(forged_raw)
                (out / "original_captions.json3").write_bytes(forged_raw)
                (out / "captions_unedited.md").write_text(forged_md, encoding="utf-8")
                with self.assertRaisesRegex(VerbatimError, "ORIGINAL_CAPTIONS_TAMPERED"):
                    export(root, mf)

    def test_f02_same_video_id_different_inputs_isolated_and_no_reuse(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000099"
            raw_v1 = b"video_input_1"
            raw_v2 = b"video_input_2_different_bytes"
            v1 = root / "original" / "vid1.mp4"
            v2 = root / "original" / "vid2.mp4"
            v1.write_bytes(raw_v1)
            v2.write_bytes(raw_v2)
            mf1_data = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(v1),
                        "size_bytes": len(raw_v1),
                        "sha256": hashlib.sha256(raw_v1).hexdigest(),
                    },
                },
            }
            mf2_data = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(v2),
                        "size_bytes": len(raw_v2),
                        "sha256": hashlib.sha256(raw_v2).hexdigest(),
                    },
                },
            }
            mf1 = root / "data" / "mf1.json"
            mf2 = root / "data" / "mf2.json"
            mf1.write_text(json.dumps(mf1_data), encoding="utf-8")
            mf2.write_text(json.dumps(mf2_data), encoding="utf-8")
            prepare(root, mf1)
            f_txt, a_txt = root / "f.txt", root / "a.txt"
            f_txt.write_text("Flash draft for input 1", encoding="utf-8")
            a_txt.write_text("Argon draft for input 1", encoding="utf-8")
            import_review(root, mf1, "flash", f_txt)
            import_review(root, mf1, "argon", a_txt)
            res1 = export(root, mf1)
            self.assertEqual(res1["status"], "VERBATIM_REVIEW_REQUIRED")
            with self.assertRaisesRegex(VerbatimError, "SOURCE_AND_BOTH_MODEL_DRAFTS_REQUIRED"):
                export(root, mf2)

    def test_f02_repeated_execution_of_same_input_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000099"
            raw_v = b"video_idempotent_test"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_v)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_v),
                        "sha256": hashlib.sha256(raw_v).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "mf_rep.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(prepare(root, mf)["status"], "VERBATIM_PREPARED")
            self.assertEqual(prepare(root, mf)["status"], "VERBATIM_PREPARED")
            f_txt, a_txt = root / "f.txt", root / "a.txt"
            f_txt.write_text("Flash idempotent", encoding="utf-8")
            a_txt.write_text("Argon idempotent", encoding="utf-8")
            import_review(root, mf, "flash", f_txt)
            import_review(root, mf, "flash", f_txt)
            import_review(root, mf, "argon", a_txt)
            import_review(root, mf, "argon", a_txt)
            self.assertEqual(export(root, mf)["status"], "VERBATIM_REVIEW_REQUIRED")
            self.assertEqual(export(root, mf)["status"], "VERBATIM_REVIEW_REQUIRED")

    def test_f03_input_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "runtime"
            root.mkdir(parents=True)
            (root / "data").mkdir()
            outside = Path(d) / "outside.mp4"
            outside.write_bytes(b"outside_content")
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000077",
                "assets": {
                    "primary_video": {
                        "path": str(outside),
                        "size_bytes": len(b"outside_content"),
                        "sha256": hashlib.sha256(b"outside_content").hexdigest(),
                    },
                },
            }
            mf = root / "data" / "escape.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "ASSET_OUTSIDE_RUNTIME"):
                inputs(root, mf)

    def test_f03_parent_directory_junction_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            real_media_dir = root / "real_media"
            real_media_dir.mkdir()
            vp = real_media_dir / "vid.mp4"
            vp.write_bytes(b"media_bytes")
            junction_dir = root / "junction_dir"
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/j", str(junction_dir), str(real_media_dir)],
                               capture_output=True, check=True)
            else:
                os.symlink(real_media_dir, junction_dir)
            junc_vp = junction_dir / "vid.mp4"
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000077",
                "assets": {
                    "primary_video": {
                        "path": str(junc_vp),
                        "size_bytes": len(b"media_bytes"),
                        "sha256": hashlib.sha256(b"media_bytes").hexdigest(),
                    },
                },
            }
            mf = root / "data" / "junc_test.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "ASSET_UNAVAILABLE"):
                inputs(root, mf)

    def test_f03_output_path_escape_or_symlink_rejected(self):
        from ytresearch.manual_verbatim import create_immutable
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "outside_target"
            target.mkdir()
            junc = root / "junc_out"
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/j", str(junc), str(target)],
                               capture_output=True, check=True)
            else:
                os.symlink(target, junc)
            with self.assertRaisesRegex(VerbatimError, "OUTPUT_SYMLINK"):
                create_immutable(junc / "file.txt", b"data")

    def test_f05_schema_conflict_and_unsupported_schema(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(b"video_bytes")
            v_sha = hashlib.sha256(b"video_bytes").hexdigest()
            # 1. Schema conflict: schema_version != contract
            mf_conflict = root / "data" / "conflict.json"
            mf_conflict.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "contract": "legacy",
                "video_id": "Tst00000077",
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": len(b"video_bytes"), "sha256": v_sha},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "SCHEMA_CONFLICT"):
                inputs(root, mf_conflict)
            # 2. Unsupported schema
            mf_unknown = root / "data" / "unknown.json"
            mf_unknown.write_text(json.dumps({
                "schema_version": "verbatim_single_v2",
                "video_id": "Tst00000077",
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": len(b"video_bytes"), "sha256": v_sha},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "SCHEMA_UNSUPPORTED"):
                inputs(root, mf_unknown)

    def test_f06_media_validation_streams_codec_corruption(self):
        from ytresearch.manual_verbatim import playback
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            p_no_audio = root / "original" / "no_audio.mp4"
            p_no_video = root / "original" / "no_video.m4a"
            p_mp3 = root / "original" / "audio_mp3.mp3"
            p_corrupt = root / "original" / "corrupt.mp4"
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=64x64:rate=10',
                            '-c:v', 'libx264', '-an', str(p_no_audio)], capture_output=True, check=True)
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'sine=duration=1:frequency=1000',
                            '-c:a', 'aac', str(p_no_video)], capture_output=True, check=True)
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'sine=duration=1:frequency=1000',
                            '-c:a', 'libmp3lame', str(p_mp3)], capture_output=True, check=True)
            p_corrupt.write_bytes(b"corrupt_media_header_bytes_12345678")

            # 1. Video without audio stream when no audio asset given
            mf_no_aud = root / "data" / "mf_no_aud.json"
            mf_no_aud.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000071",
                "assets": {
                    "primary_video": {"path": str(p_no_audio), "size_bytes": p_no_audio.stat().st_size, "sha256": hashlib.sha256(p_no_audio.read_bytes()).hexdigest()},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "STREAM_MISSING"):
                playback(root, mf_no_aud)

            # 2. No video stream in primary_video
            mf_no_vid = root / "data" / "mf_no_vid.json"
            mf_no_vid.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000072",
                "assets": {
                    "primary_video": {"path": str(p_no_video), "size_bytes": p_no_video.stat().st_size, "sha256": hashlib.sha256(p_no_video.read_bytes()).hexdigest()},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "STREAM_MISSING"):
                playback(root, mf_no_vid)

            # 3. Non-AAC audio track
            mf_non_aac = root / "data" / "mf_non_aac.json"
            mf_non_aac.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000073",
                "assets": {
                    "primary_video": {"path": str(p_no_audio), "size_bytes": p_no_audio.stat().st_size, "sha256": hashlib.sha256(p_no_audio.read_bytes()).hexdigest()},
                    "audio": {"path": str(p_mp3), "size_bytes": p_mp3.stat().st_size, "sha256": hashlib.sha256(p_mp3.read_bytes()).hexdigest()},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "AUDIO_CODEC_UNSUPPORTED"):
                playback(root, mf_non_aac)

            # 4. Corrupted media
            mf_corrupt = root / "data" / "mf_corrupt.json"
            mf_corrupt.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000074",
                "assets": {
                    "primary_video": {"path": str(p_corrupt), "size_bytes": p_corrupt.stat().st_size, "sha256": hashlib.sha256(p_corrupt.read_bytes()).hexdigest()},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "MEDIA_CORRUPTED"):
                playback(root, mf_corrupt)

    def test_f07_prompt_contract_no_captions_does_not_demand_json3(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000099"
            raw_v = b"simulated_video"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_v)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {
                        "path": str(vp),
                        "size_bytes": len(raw_v),
                        "sha256": hashlib.sha256(raw_v).hexdigest(),
                    },
                },
            }
            mf = root / "data" / "single_no_sub.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            prepare(root, mf)
            _, out = inputs(root, mf)
            argon_prompt = (out / "ARGON_INSTRUCTIONS.txt").read_text(encoding="utf-8")
            self.assertNotIn("Flash 전사 및 JSON3를 함께 제공하세요", argon_prompt)

    def test_g01_screens_directory_symlink_or_junction_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            outside = root / "outside_dir"
            outside.mkdir()
            vid = "Tst00000081"
            vp = root / "original" / "vid.mp4"
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=64x64:rate=10',
                            '-c:v', 'libx264', '-an', str(vp)], capture_output=True, check=True)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": vp.stat().st_size, "sha256": hashlib.sha256(vp.read_bytes()).hexdigest()},
                },
            }
            mf = root / "data" / "mf.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            prepare(root, mf)
            _, out = inputs(root, mf)
            screens_link = out / "screens"
            screens_link.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/j", str(screens_link), str(outside)],
                               capture_output=True, check=True)
            else:
                os.symlink(outside, screens_link)
            from ytresearch.manual_verbatim import screen, export
            with self.assertRaisesRegex(VerbatimError, "OUTPUT_SYMLINK|OUTPUT_OUTSIDE_RUNTIME"):
                screen(root, mf, 0.0)
            f_txt, a_txt = root / "f.txt", root / "a.txt"
            f_txt.write_text("flash", encoding="utf-8")
            a_txt.write_text("argon", encoding="utf-8")
            import_review(root, mf, "flash", f_txt)
            import_review(root, mf, "argon", a_txt)
            with self.assertRaisesRegex(VerbatimError, "OUTPUT_SYMLINK|OUTPUT_OUTSIDE_RUNTIME"):
                export(root, mf)

    def test_g02_execution_manifest_validation_and_review_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000082"
            raw_v = b"vid_content_g02_test"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_v)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": len(raw_v), "sha256": hashlib.sha256(raw_v).hexdigest()},
                },
            }
            mf = root / "data" / "mf.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            prepare(root, mf)
            _, out = inputs(root, mf)
            exec_mf = out / "execution_manifest.json"
            self.assertTrue(exec_mf.is_file())

            # 1. Tampered execution manifest
            exec_data = json.loads(exec_mf.read_text(encoding="utf-8"))
            exec_data["input_digest"] = "tampered_digest_0000000000000000"
            exec_mf.unlink()
            exec_mf.write_text(json.dumps(exec_data), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "EXECUTION_MANIFEST_TAMPERED"):
                inputs(root, mf)

            # 2. Copied execution manifest from another input
            vid2 = "Tst00000083"
            raw_v2 = b"vid_content_g02_another"
            vp2 = root / "original" / "vid2.mp4"
            vp2.write_bytes(raw_v2)
            mf2_obj = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid2,
                "assets": {
                    "primary_video": {"path": str(vp2), "size_bytes": len(raw_v2), "sha256": hashlib.sha256(raw_v2).hexdigest()},
                },
            }
            mf2 = root / "data" / "mf2.json"
            mf2.write_text(json.dumps(mf2_obj), encoding="utf-8")
            _, out2 = inputs(root, mf2)
            out2.mkdir(parents=True, exist_ok=True)
            # Copy exec_data from first input to second
            (out2 / "execution_manifest.json").write_text(json.dumps(exec_data), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "EXECUTION_MANIFEST_TAMPERED"):
                inputs(root, mf2)

            # 3. Missing execution manifest when outputs exist
            (out2 / "execution_manifest.json").unlink()
            (out2 / "flash_unverified.txt").write_text("some output", encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "EXECUTION_MANIFEST_MISSING"):
                inputs(root, mf2)

            # 4. Review reuse forbidden when copied without valid review manifest
            (out2 / "flash_unverified.txt").unlink()
            prepare(root, mf2)
            f_txt, a_txt = root / "f.txt", root / "a.txt"
            f_txt.write_text("flash review", encoding="utf-8")
            a_txt.write_text("argon review", encoding="utf-8")
            import_review(root, mf2, "flash", f_txt)
            (out2 / "argon_unverified.txt").write_text("copied argon", encoding="utf-8")
            from ytresearch.manual_verbatim import export
            with self.assertRaisesRegex(VerbatimError, "REVIEW_REUSE_FORBIDDEN"):
                export(root, mf2)

    def test_g03_public_git_runtime_forbidden(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "fake_repo" / "runtime"
            root.mkdir(parents=True)
            (Path(d) / "fake_repo" / ".git").mkdir()
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000084"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(b"content")
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": 7, "sha256": hashlib.sha256(b"content").hexdigest()},
                },
            }
            mf = root / "data" / "mf.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "PUBLIC_GIT_RUNTIME_FORBIDDEN"):
                inputs(root, mf)
            self.assertFalse((root / "data" / "verbatim").exists())

    def test_g04_relative_and_absolute_manifest_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000085"
            raw_v = b"vid_content_g04"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_v)
            manifest = {
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {"path": str(vp), "size_bytes": len(raw_v), "sha256": hashlib.sha256(raw_v).hexdigest()},
                },
            }
            mf = root / "data" / "relative_manifest.json"
            mf.write_text(json.dumps(manifest), encoding="utf-8")
            old_cwd = os.getcwd()
            try:
                # Change CWD away from root
                os.chdir(d)
                # Test relative path
                res = prepare(root, "data/relative_manifest.json")
                self.assertEqual(res["status"], "VERBATIM_PREPARED")
                # Test absolute path
                res_abs = prepare(root, mf.resolve())
                self.assertEqual(res_abs["status"], "VERBATIM_PREPARED")
            finally:
                os.chdir(old_cwd)

    def test_g05_schema_values_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vid = "Tst00000086"
            raw_v = b"vid_content_g05"
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(raw_v)
            base_assets = {
                "primary_video": {"path": str(vp), "size_bytes": len(raw_v), "sha256": hashlib.sha256(raw_v).hexdigest()},
            }
            # 1. Empty string schema
            mf1 = root / "data" / "mf1.json"
            mf1.write_text(json.dumps({"schema_version": "", "video_id": vid, "assets": base_assets}), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "SCHEMA_UNSUPPORTED"):
                inputs(root, mf1)

            # 2. Number schema
            mf2 = root / "data" / "mf2.json"
            mf2.write_text(json.dumps({"schema_version": 123, "video_id": vid, "assets": base_assets}), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "SCHEMA_UNSUPPORTED"):
                inputs(root, mf2)

            # 3. Boolean schema
            mf3 = root / "data" / "mf3.json"
            mf3.write_text(json.dumps({"schema_version": True, "video_id": vid, "assets": base_assets}), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "SCHEMA_UNSUPPORTED"):
                inputs(root, mf3)

            # 4. Null schema
            mf4 = root / "data" / "mf4.json"
            mf4.write_text(json.dumps({"schema_version": None, "video_id": vid, "assets": base_assets}), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "SCHEMA_UNSUPPORTED"):
                inputs(root, mf4)

            # 5. Conflicting schema_version and contract
            mf5 = root / "data" / "mf5.json"
            mf5.write_text(json.dumps({"schema_version": "verbatim_single_v1", "contract": "legacy", "video_id": vid, "assets": base_assets}), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "SCHEMA_CONFLICT"):
                inputs(root, mf5)

            # 6. Path traversal in asset path
            mf6 = root / "data" / "mf6.json"
            mf6.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": vid,
                "assets": {
                    "primary_video": {"path": "data/../original/vid.mp4", "size_bytes": len(raw_v), "sha256": hashlib.sha256(raw_v).hexdigest()},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "PATH_TRAVERSAL_FORBIDDEN"):
                inputs(root, mf6)

            # 7. Path traversal in manifest path
            with self.assertRaisesRegex(VerbatimError, "PATH_TRAVERSAL_FORBIDDEN"):
                inputs(root, "data/../data/mf1.json")

    def test_g06_ffmpeg_normal_media_playback_and_compatible(self):
        import av
        from ytresearch.manual_verbatim import playback, playback_compatible
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            p_embedded = root / "original" / "embedded_h264_aac.mp4"
            p_vid_only = root / "original" / "vid_only.mp4"
            p_aud_only = root / "original" / "aud_only.m4a"

            # A. H.264 + embedded AAC MP4
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=64x64:rate=10',
                            '-f', 'lavfi', '-i', 'sine=duration=1:frequency=1000',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(p_embedded)],
                           capture_output=True, check=True)
            # B. Video only + separate AAC audio
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=64x64:rate=10',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', str(p_vid_only)],
                           capture_output=True, check=True)
            subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'sine=duration=1:frequency=1000',
                            '-c:a', 'aac', str(p_aud_only)],
                           capture_output=True, check=True)

            # Test A: playback & playback_compatible on embedded media
            mf_a = root / "data" / "mf_embedded.json"
            mf_a.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000087",
                "assets": {
                    "primary_video": {"path": str(p_embedded), "size_bytes": p_embedded.stat().st_size, "sha256": hashlib.sha256(p_embedded.read_bytes()).hexdigest()},
                },
            }), encoding="utf-8")
            res_pb_a = playback(root, mf_a)
            self.assertEqual(res_pb_a["status"], "PLAYBACK_CREATED")
            _, out_a = inputs(root, mf_a)
            pb_mp4_a = out_a / "playback_with_audio.mp4"
            self.assertTrue(pb_mp4_a.is_file())
            with av.open(str(pb_mp4_a)) as container:
                self.assertEqual(len(container.streams.video), 1)
                self.assertEqual(len(container.streams.audio), 1)
                self.assertEqual(container.streams.video[0].codec_context.name, "h264")
                self.assertEqual(container.streams.audio[0].codec_context.name, "aac")
                self.assertIsNotNone(next(container.decode(video=0), None))
                self.assertIsNotNone(next(container.decode(audio=0), None))

            res_pc_a = playback_compatible(root, mf_a)
            self.assertEqual(res_pc_a["status"], "PLAYBACK_COMPATIBLE_CREATED")
            self.assertTrue(res_pc_a["audio_copied"])
            pc_mp4_a = out_a / "playback_h264_aac.mp4"
            self.assertTrue(pc_mp4_a.is_file())
            with av.open(str(pc_mp4_a)) as container:
                self.assertEqual(len(container.streams.video), 1)
                self.assertEqual(len(container.streams.audio), 1)
                self.assertEqual(container.streams.video[0].codec_context.name, "h264")
                self.assertEqual(container.streams.audio[0].codec_context.name, "aac")
                self.assertIsNotNone(next(container.decode(video=0), None))
                self.assertIsNotNone(next(container.decode(audio=0), None))

            # Test B: playback & playback_compatible on video + separate AAC
            mf_b = root / "data" / "mf_separate.json"
            mf_b.write_text(json.dumps({
                "schema_version": "verbatim_single_v1",
                "video_id": "Tst00000088",
                "assets": {
                    "primary_video": {"path": str(p_vid_only), "size_bytes": p_vid_only.stat().st_size, "sha256": hashlib.sha256(p_vid_only.read_bytes()).hexdigest()},
                    "audio": {"path": str(p_aud_only), "size_bytes": p_aud_only.stat().st_size, "sha256": hashlib.sha256(p_aud_only.read_bytes()).hexdigest()},
                },
            }), encoding="utf-8")
            res_pb_b = playback(root, mf_b)
            self.assertEqual(res_pb_b["status"], "PLAYBACK_CREATED")
            _, out_b = inputs(root, mf_b)
            pb_mp4_b = out_b / "playback_with_audio.mp4"
            self.assertTrue(pb_mp4_b.is_file())
            with av.open(str(pb_mp4_b)) as container:
                self.assertEqual(len(container.streams.video), 1)
                self.assertEqual(len(container.streams.audio), 1)
                self.assertEqual(container.streams.video[0].codec_context.name, "h264")
                self.assertEqual(container.streams.audio[0].codec_context.name, "aac")
                self.assertIsNotNone(next(container.decode(video=0), None))
                self.assertIsNotNone(next(container.decode(audio=0), None))

            res_pc_b = playback_compatible(root, mf_b)
            self.assertEqual(res_pc_b["status"], "PLAYBACK_COMPATIBLE_CREATED")
            self.assertTrue(res_pc_b["audio_copied"])
            pc_mp4_b = out_b / "playback_h264_aac.mp4"
            self.assertTrue(pc_mp4_b.is_file())
            with av.open(str(pc_mp4_b)) as container:
                self.assertEqual(len(container.streams.video), 1)
                self.assertEqual(len(container.streams.audio), 1)
                self.assertEqual(container.streams.video[0].codec_context.name, "h264")
                self.assertEqual(container.streams.audio[0].codec_context.name, "aac")
                self.assertIsNotNone(next(container.decode(video=0), None))
                self.assertIsNotNone(next(container.decode(audio=0), None))

    def test_g07_legacy_compatibility(self):
        with tempfile.TemporaryDirectory() as d:
            root, mf, _ = self.make_files(d)
            with patch("ytresearch.manual_verbatim.read_source_ledger",
                       return_value={"videos": [{"video_id": "Tst00000001"}]}):
                found, out = inputs(root, mf)
                self.assertEqual(set(found.keys()), {"primary_video", "highres_video", "audio", "subtitle_json3"})
                # Legacy output directory must be data/verbatim/<vid> without input_digest
                self.assertEqual(out, (root / "data" / "verbatim" / "Tst00000001").resolve())
                prepare(root, mf)
                # Legacy does not write execution_manifest.json
                self.assertFalse((out / "execution_manifest.json").exists())

    @staticmethod
    def _make_dir_link(link_path: Path, target_path: Path) -> None:
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/j", str(link_path), str(target_path)],
                capture_output=True,
                check=True,
            )
        else:
            os.symlink(target_path, link_path)

    def test_d01_prepare_never_reads_manifest_before_boundary_checks(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            read_paths: list[str] = []
            open_paths: list[str] = []
            orig_read_text = Path.read_text
            orig_read_bytes = Path.read_bytes
            orig_open = Path.open

            def tracked_read_text(path_self, *args, **kwargs):
                read_paths.append(str(path_self))
                read_paths.append(str(path_self.resolve(strict=False)))
                return orig_read_text(path_self, *args, **kwargs)

            def tracked_read_bytes(path_self, *args, **kwargs):
                read_paths.append(str(path_self))
                read_paths.append(str(path_self.resolve(strict=False)))
                return orig_read_bytes(path_self, *args, **kwargs)

            def tracked_open(path_self, *args, **kwargs):
                open_paths.append(str(path_self))
                open_paths.append(str(path_self.resolve(strict=False)))
                return orig_open(path_self, *args, **kwargs)

            # 1. External file outside Runtime
            rt1 = base / "rt1"
            (rt1 / "data").mkdir(parents=True)
            outside_mf = base / "outside_secret.json"
            outside_mf.write_text(
                json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000091",
                    "assets": {},
                }),
                encoding="utf-8",
            )

            # 2. External JSON via Junction/symlink inside data/
            outside_dir = base / "outside_junc_dir"
            outside_dir.mkdir()
            secret_mf = outside_dir / "secret.json"
            secret_mf.write_text(
                json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000092",
                    "assets": {},
                }),
                encoding="utf-8",
            )
            junc_dir = rt1 / "data" / "junc_ext"
            self._make_dir_link(junc_dir, outside_dir)
            junc_mf = junc_dir / "secret.json"

            # 3. Manifest inside Git worktree
            repo = base / "fake_repo"
            (repo / ".git").mkdir(parents=True)
            git_rt = repo / "runtime"
            (git_rt / "data").mkdir(parents=True)
            git_mf = git_rt / "data" / "mf.json"
            git_mf.write_text(
                json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000093",
                    "assets": {},
                }),
                encoding="utf-8",
            )

            # 4. Valid Runtime manifest for read-count check
            clean_rt = base / "clean_rt"
            (clean_rt / "data").mkdir(parents=True)
            (clean_rt / "original").mkdir()
            vp = clean_rt / "original" / "vid.mp4"
            raw_v = b"valid_video_bytes_d01"
            vp.write_bytes(raw_v)
            valid_mf = clean_rt / "data" / "valid.json"
            valid_mf.write_text(
                json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000094",
                    "assets": {
                        "primary_video": {
                            "path": str(vp),
                            "size_bytes": len(raw_v),
                            "sha256": hashlib.sha256(raw_v).hexdigest(),
                        },
                    },
                }),
                encoding="utf-8",
            )
            flash_txt = clean_rt / "flash.txt"
            argon_txt = clean_rt / "argon.txt"
            flash_txt.write_text("flash draft", encoding="utf-8")
            argon_txt.write_text("argon draft", encoding="utf-8")

            with (
                patch.object(Path, "read_text", tracked_read_text),
                patch.object(Path, "read_bytes", tracked_read_bytes),
                patch.object(Path, "open", tracked_open),
            ):
                read_paths.clear()
                open_paths.clear()
                with self.assertRaisesRegex(VerbatimError, "MANIFEST_OUTSIDE_RUNTIME|MANIFEST_NOT_FOUND"):
                    prepare(rt1, outside_mf)
                self.assertNotIn(str(outside_mf), read_paths)
                self.assertNotIn(str(outside_mf.resolve()), read_paths)
                self.assertNotIn(str(outside_mf), open_paths)
                self.assertNotIn(str(outside_mf.resolve()), open_paths)

                read_paths.clear()
                open_paths.clear()
                with self.assertRaises(VerbatimError):
                    prepare(rt1, junc_mf)
                for candidate in (junc_mf, junc_mf.resolve(), secret_mf, secret_mf.resolve()):
                    self.assertNotIn(str(candidate), read_paths)
                    self.assertNotIn(str(candidate), open_paths)

                read_paths.clear()
                open_paths.clear()
                with self.assertRaisesRegex(VerbatimError, "PUBLIC_GIT_RUNTIME_FORBIDDEN"):
                    prepare(git_rt, git_mf)
                self.assertNotIn(str(git_mf), read_paths)
                self.assertNotIn(str(git_mf.resolve()), read_paths)
                self.assertNotIn(str(git_mf), open_paths)
                self.assertNotIn(str(git_mf.resolve()), open_paths)

                # Valid calls must read manifest at most once each
                resolved_valid_mf = str(valid_mf.resolve())
                for fn in (
                    lambda: prepare(clean_rt, valid_mf),
                    lambda: import_review(clean_rt, valid_mf, "flash", flash_txt),
                    lambda: import_review(clean_rt, valid_mf, "argon", argon_txt),
                    lambda: export(clean_rt, valid_mf),
                ):
                    open_paths.clear()
                    fn()
                    mf_reads = sum(1 for p in open_paths if p == resolved_valid_mf)
                    self.assertLessEqual(mf_reads, 2)  # raw + resolved pushed per open
                    # More precisely, count via read_text calls or half of pair:
                # Direct open count via single-entry list
            open_call_targets: list[Path] = []

            def single_tracked_open(path_self, *args, **kwargs):
                open_call_targets.append(path_self.resolve(strict=False))
                return orig_open(path_self, *args, **kwargs)

            clean_rt2 = base / "clean_rt2"
            (clean_rt2 / "data").mkdir(parents=True)
            (clean_rt2 / "original").mkdir()
            vp2 = clean_rt2 / "original" / "vid.mp4"
            vp2.write_bytes(raw_v)
            valid_mf2 = clean_rt2 / "data" / "valid.json"
            valid_mf2.write_text(
                json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000095",
                    "assets": {
                        "primary_video": {
                            "path": str(vp2),
                            "size_bytes": len(raw_v),
                            "sha256": hashlib.sha256(raw_v).hexdigest(),
                        },
                    },
                }),
                encoding="utf-8",
            )
            f2 = clean_rt2 / "flash.txt"
            a2 = clean_rt2 / "argon.txt"
            f2.write_text("flash draft", encoding="utf-8")
            a2.write_text("argon draft", encoding="utf-8")

            with patch.object(Path, "open", single_tracked_open):
                for fn in (
                    lambda: prepare(clean_rt2, valid_mf2),
                    lambda: import_review(clean_rt2, valid_mf2, "flash", f2),
                    lambda: import_review(clean_rt2, valid_mf2, "argon", a2),
                    lambda: export(clean_rt2, valid_mf2),
                ):
                    open_call_targets.clear()
                    fn()
                    self.assertEqual(
                        sum(1 for p in open_call_targets if p == valid_mf2.resolve()),
                        1,
                    )

    def test_d02_junction_escape_blocked_for_all_public_functions(self):
        from ytresearch.manual_verbatim import playback, playback_compatible, screen

        def assert_no_external_writes(outside_dir: Path, baseline_files: set[Path]):
            self.assertEqual(set(outside_dir.rglob("*")), baseline_files)

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)

            def setup_media_runtime(rt: Path, vid: str):
                (rt / "original").mkdir(parents=True)
                vp = rt / "original" / "embedded.mp4"
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-f", "lavfi",
                        "-i", "testsrc=duration=1:size=64x64:rate=10",
                        "-f", "lavfi", "-i", "sine=duration=1:frequency=1000",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        str(vp),
                    ],
                    capture_output=True,
                    check=True,
                )
                sub = rt / "original" / "sub.json3"
                sub.write_text(
                    json.dumps({"events": [{"tStartMs": 100, "dDurationMs": 500, "segs": [{"utf8": "hello"}]}]}),
                    encoding="utf-8",
                )
                flash_p = rt / "flash.txt"
                argon_p = rt / "argon.txt"
                flash_p.write_text("flash text", encoding="utf-8")
                argon_p.write_text("argon text", encoding="utf-8")
                mf_obj = {
                    "schema_version": "verbatim_single_v1",
                    "video_id": vid,
                    "assets": {
                        "primary_video": {
                            "path": str(vp),
                            "size_bytes": vp.stat().st_size,
                            "sha256": hashlib.sha256(vp.read_bytes()).hexdigest(),
                        },
                        "subtitle_json3": {
                            "path": str(sub),
                            "size_bytes": sub.stat().st_size,
                            "sha256": hashlib.sha256(sub.read_bytes()).hexdigest(),
                        },
                    },
                }
                return mf_obj, flash_p, argon_p

            # Case A: root/data is a Junction/symlink -> outside_a
            rt_a = base / "rt_a"
            rt_a.mkdir()
            outside_a = base / "outside_a"
            outside_a.mkdir()
            mf_obj_a, flash_a, argon_a = setup_media_runtime(rt_a, "Tst00000101")
            mf_ext_a = outside_a / "mf.json"
            mf_ext_a.write_text(json.dumps(mf_obj_a), encoding="utf-8")
            self._make_dir_link(rt_a / "data", outside_a)
            mf_link_a = rt_a / "data" / "mf.json"
            baseline_a = {mf_ext_a}

            for mf_arg in (mf_ext_a, mf_link_a):
                for fn in (
                    lambda m=mf_arg: prepare(rt_a, m),
                    lambda m=mf_arg: playback(rt_a, m),
                    lambda m=mf_arg: playback_compatible(rt_a, m),
                    lambda m=mf_arg: screen(rt_a, m, 0.0),
                    lambda m=mf_arg: import_review(rt_a, m, "flash", flash_a),
                    lambda m=mf_arg: export(rt_a, m),
                ):
                    with self.assertRaises(VerbatimError):
                        fn()
                    assert_no_external_writes(outside_a, baseline_a)

            # Case B: root/data real dir, root/data/verbatim Junction/symlink -> outside_b
            rt_b = base / "rt_b"
            (rt_b / "data").mkdir(parents=True)
            outside_b = base / "outside_b"
            outside_b.mkdir()
            mf_obj_b, flash_b, _ = setup_media_runtime(rt_b, "Tst00000102")
            mf_b = rt_b / "data" / "mf.json"
            mf_b.write_text(json.dumps(mf_obj_b), encoding="utf-8")
            self._make_dir_link(rt_b / "data" / "verbatim", outside_b)
            baseline_b: set[Path] = set()

            for fn in (
                lambda: prepare(rt_b, mf_b),
                lambda: playback(rt_b, mf_b),
                lambda: playback_compatible(rt_b, mf_b),
                lambda: screen(rt_b, mf_b, 0.0),
                lambda: import_review(rt_b, mf_b, "flash", flash_b),
                lambda: export(rt_b, mf_b),
            ):
                with self.assertRaisesRegex(VerbatimError, "OUTPUT_SYMLINK|OUTPUT_OUTSIDE_RUNTIME"):
                    fn()
                assert_no_external_writes(outside_b, baseline_b)

            # Case C: screens subdirectory Junction/symlink -> outside_c
            rt_c = base / "rt_c"
            (rt_c / "data").mkdir(parents=True)
            outside_c = base / "outside_c"
            outside_c.mkdir()
            mf_obj_c, flash_c, argon_c = setup_media_runtime(rt_c, "Tst00000103")
            mf_c = rt_c / "data" / "mf.json"
            mf_c.write_text(json.dumps(mf_obj_c), encoding="utf-8")
            prepare(rt_c, mf_c)
            import_review(rt_c, mf_c, "flash", flash_c)
            import_review(rt_c, mf_c, "argon", argon_c)
            _, out_c = inputs(rt_c, mf_c)
            self._make_dir_link(out_c / "screens", outside_c)
            with self.assertRaisesRegex(VerbatimError, "OUTPUT_SYMLINK|OUTPUT_OUTSIDE_RUNTIME"):
                screen(rt_c, mf_c, 0.0)
            assert_no_external_writes(outside_c, set())
            with self.assertRaisesRegex(VerbatimError, "OUTPUT_SYMLINK|OUTPUT_OUTSIDE_RUNTIME"):
                export(rt_c, mf_c)
            assert_no_external_writes(outside_c, set())

            # Case D: Normal Runtime without links succeeds for all 6 functions
            rt_d = base / "rt_d"
            (rt_d / "data").mkdir(parents=True)
            mf_obj_d, flash_d, argon_d = setup_media_runtime(rt_d, "Tst00000104")
            mf_d = rt_d / "data" / "mf.json"
            mf_d.write_text(json.dumps(mf_obj_d), encoding="utf-8")
            self.assertEqual(prepare(rt_d, mf_d)["status"], "VERBATIM_PREPARED")
            self.assertEqual(playback(rt_d, mf_d)["status"], "PLAYBACK_CREATED")
            self.assertEqual(playback_compatible(rt_d, mf_d)["status"], "PLAYBACK_COMPATIBLE_CREATED")
            self.assertEqual(screen(rt_d, mf_d, 0.0)["status"], "SCREEN_SAVED")
            self.assertEqual(import_review(rt_d, mf_d, "flash", flash_d)["status"], "UNVERIFIED_REVIEW_STORED")
            self.assertEqual(import_review(rt_d, mf_d, "argon", argon_d)["status"], "UNVERIFIED_REVIEW_STORED")
            res_exp = export(rt_d, mf_d)
            self.assertEqual(res_exp["status"], "VERBATIM_REVIEW_REQUIRED")
            self.assertEqual(res_exp["screenshots"], 1)

    def test_d04_prepare_interruption_and_idempotent_retry(self):
        import ytresearch.manual_verbatim as mv

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)

            def build_case(rt: Path, events: list[dict] | None = None):
                (rt / "data").mkdir(parents=True)
                (rt / "original").mkdir()
                vp = rt / "original" / "vid.mp4"
                sp = rt / "original" / "sub.json3"
                vp.write_bytes(b"video_bytes_d04")
                if events is None:
                    events = [{"tStartMs": 100, "dDurationMs": 500, "segs": [{"utf8": "line one"}]}]
                sp.write_text(json.dumps({"events": events}), encoding="utf-8")
                mf = rt / "data" / "mf.json"
                mf.write_text(
                    json.dumps({
                        "schema_version": "verbatim_single_v1",
                        "video_id": "Tst00000111",
                        "assets": {
                            "primary_video": {
                                "path": str(vp),
                                "size_bytes": vp.stat().st_size,
                                "sha256": hashlib.sha256(vp.read_bytes()).hexdigest(),
                            },
                            "subtitle_json3": {
                                "path": str(sp),
                                "size_bytes": sp.stat().st_size,
                                "sha256": hashlib.sha256(sp.read_bytes()).hexdigest(),
                            },
                        },
                    }),
                    encoding="utf-8",
                )
                return mf

            # Uninterrupted baseline
            clean_rt = base / "clean"
            clean_mf = build_case(clean_rt)
            expected_res = prepare(clean_rt, clean_mf)
            _, clean_out = inputs(clean_rt, clean_mf)
            baseline_hashes = {
                p.relative_to(clean_out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(clean_out.rglob("*"))
                if p.is_file()
            }

            # Case 1: Fail on 2nd artifact creation
            rt1 = base / "rt1"
            mf1 = build_case(rt1)
            _, out1 = inputs(rt1, mf1)
            orig_create = mv.create_immutable
            call_count = {"n": 0}

            def fail_on_second(path, raw, **kwargs):
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise OSError("simulated mid-prepare I/O failure")
                return orig_create(path, raw, **kwargs)

            with patch("ytresearch.manual_verbatim.create_immutable", side_effect=fail_on_second):
                with self.assertRaises(OSError):
                    prepare(rt1, mf1)

            self.assertTrue((out1 / "execution_manifest.json").is_file())
            self.assertTrue((out1 / "original_captions.json3").is_file())
            retry_res = prepare(rt1, mf1)
            self.assertEqual(retry_res, expected_res)
            actual_hashes_1 = {
                p.relative_to(out1).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(out1.rglob("*"))
                if p.is_file()
            }
            self.assertEqual(actual_hashes_1, baseline_hashes)

            # Case 2: Fail during execution_manifest.json os.link once
            rt2 = base / "rt2"
            mf2 = build_case(rt2)
            _, out2 = inputs(rt2, mf2)
            orig_link = os.link
            link_state = {"failed": False}

            def fail_link_once(src, dst):
                if not link_state["failed"] and Path(dst).name == "execution_manifest.json":
                    link_state["failed"] = True
                    raise OSError("simulated manifest link failure")
                return orig_link(src, dst)

            with patch("ytresearch.manual_verbatim.os.link", side_effect=fail_link_once):
                with self.assertRaises(OSError):
                    prepare(rt2, mf2)

            self.assertTrue(link_state["failed"])
            self.assertFalse((out2 / "execution_manifest.json").exists())
            if out2.exists():
                self.assertEqual(list(out2.rglob("*")), [])
            retry_res_2 = prepare(rt2, mf2)
            self.assertEqual(retry_res_2, expected_res)
            actual_hashes_2 = {
                p.relative_to(out2).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(out2.rglob("*"))
                if p.is_file()
            }
            self.assertEqual(actual_hashes_2, baseline_hashes)

            # Case 3: Invalid captions fail BEFORE writing execution_manifest.json
            rt3 = base / "rt3"
            mf3 = build_case(rt3, events=[])
            _, out3 = inputs(rt3, mf3)
            with self.assertRaisesRegex(VerbatimError, "JSON3_EMPTY_TEXT"):
                prepare(rt3, mf3)
            self.assertFalse((out3 / "execution_manifest.json").exists())

            # Case 4: Tampered execution_manifest.json blocked without deleting existing artifacts
            exec_mf = out1 / "execution_manifest.json"
            exec_data = json.loads(exec_mf.read_text(encoding="utf-8"))
            exec_data["video_id"] = "Tampered001"
            exec_mf.write_text(json.dumps(exec_data), encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "EXECUTION_MANIFEST_TAMPERED"):
                prepare(rt1, mf1)
            self.assertTrue((out1 / "FLASH_INSTRUCTIONS.txt").is_file())

    def test_d05_non_object_json_raises_verbatim_error_not_attribute_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            vp = root / "original" / "vid.mp4"
            vp.write_bytes(b"vid_bytes_d05")
            for idx, payload in enumerate(("[]", '"text"', "123", "null")):
                mf = root / "data" / f"non_obj_{idx}.json"
                mf.write_text(payload, encoding="utf-8")
                with self.assertRaises(VerbatimError):
                    inputs(root, mf)
                with self.assertRaises(VerbatimError):
                    prepare(root, mf)
                with self.assertRaisesRegex(VerbatimError, "JSON3_EVENTS_INVALID"):
                    render_captions(payload.encode("utf-8"))

            valid_mf = root / "data" / "valid.json"
            valid_mf.write_text(
                json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000121",
                    "assets": {
                        "primary_video": {
                            "path": str(vp),
                            "size_bytes": vp.stat().st_size,
                            "sha256": hashlib.sha256(vp.read_bytes()).hexdigest(),
                        },
                    },
                }),
                encoding="utf-8",
            )
            prepare(root, valid_mf)
            f_txt = root / "f.txt"
            a_txt = root / "a.txt"
            f_txt.write_text("flash", encoding="utf-8")
            a_txt.write_text("argon", encoding="utf-8")
            import_review(root, valid_mf, "flash", f_txt)
            import_review(root, valid_mf, "argon", a_txt)
            _, out = inputs(root, valid_mf)

            # Overwrite flash_review_manifest.json with non-object JSON
            rev_mf = out / "flash_review_manifest.json"
            rev_mf.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "REVIEW_REUSE_FORBIDDEN"):
                export(root, valid_mf)

            # Overwrite execution_manifest.json with non-object JSON
            exec_mf = out / "execution_manifest.json"
            exec_mf.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(VerbatimError, "EXECUTION_MANIFEST_TAMPERED"):
                inputs(root, valid_mf)

    def test_d06_invalid_media_does_not_write_execution_manifest(self):
        from ytresearch.manual_verbatim import playback, playback_compatible

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "original").mkdir()
            p_corrupt = root / "original" / "corrupt.mp4"
            p_corrupt.write_bytes(b"corrupt_media_header_bytes_12345678")
            p_no_audio = root / "original" / "no_audio.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=1:size=64x64:rate=10",
                    "-c:v", "libx264", "-an", str(p_no_audio),
                ],
                capture_output=True,
                check=True,
            )

            mf_corrupt = root / "data" / "mf_corrupt.json"
            mf_corrupt.write_text(
                json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000131",
                    "assets": {
                        "primary_video": {
                            "path": str(p_corrupt),
                            "size_bytes": p_corrupt.stat().st_size,
                            "sha256": hashlib.sha256(p_corrupt.read_bytes()).hexdigest(),
                        },
                    },
                }),
                encoding="utf-8",
            )
            _, out_corrupt = inputs(root, mf_corrupt)
            with self.assertRaisesRegex(VerbatimError, "MEDIA_CORRUPTED"):
                playback(root, mf_corrupt)
            self.assertFalse((out_corrupt / "execution_manifest.json").exists())

            with self.assertRaisesRegex(VerbatimError, "MEDIA_CORRUPTED"):
                playback_compatible(root, mf_corrupt)
            self.assertFalse((out_corrupt / "execution_manifest.json").exists())

            mf_no_aud = root / "data" / "mf_no_aud.json"
            mf_no_aud.write_text(
                json.dumps({
                    "schema_version": "verbatim_single_v1",
                    "video_id": "Tst00000132",
                    "assets": {
                        "primary_video": {
                            "path": str(p_no_audio),
                            "size_bytes": p_no_audio.stat().st_size,
                            "sha256": hashlib.sha256(p_no_audio.read_bytes()).hexdigest(),
                        },
                    },
                }),
                encoding="utf-8",
            )
            _, out_no_aud = inputs(root, mf_no_aud)
            with self.assertRaisesRegex(VerbatimError, "STREAM_MISSING"):
                playback_compatible(root, mf_no_aud)
            self.assertFalse((out_no_aud / "execution_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
