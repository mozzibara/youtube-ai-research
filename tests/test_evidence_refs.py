"""Regression tests for rendered ROI evidence-panel numbering and fail-closed checks."""
from __future__ import annotations
import unittest
from ytresearch.evidence_refs import (
    InvalidEvidenceReference, build_panel_catalog, reconcile_analysis_evidence,
)


def manifest():
    boards = []
    for number in range(1, 17):
        x = number * 2
        boards.append({
            "board_id": f"board_{number:02d}",
            "path": f"/boards/board_{number:02d}.png",
            "panel_frame_ids": [f"frame_{x-1:03d}", f"frame_{x:03d}"],
            "timestamps_sec": [float(x-1), float(x)],
        })
    for k, axis in enumerate(("left", "center", "right")):
        for sub in range(2):
            number = k * 2 + sub + 1
            fr = sub * 2 + 1
            boards.append({
                "board_id": f"roi_board_{number:02d}",
                "path": f"/boards/roi_board_{number:02d}.png",
                "panel_frame_ids": [f"roi_{axis}_{fr:03d}", f"roi_{axis}_{fr+1:03d}"],
                "timestamps_sec": [float(number*2-1), float(number*2)],
            })
    return {"boards": boards}


class RenderedEvidenceReferenceTests(unittest.TestCase):
    def test_actual_roi_headers_use_continuous_1_to_12_panel_numbers(self):
        catalog = build_panel_catalog(manifest())
        for board in range(1, 7):
            expected = [2 * board - 1, 2 * board]
            self.assertEqual(
                sorted(catalog[f"roi_board_{board:02d}.png"]["panels"]), expected,
            )
        self.assertEqual(
            catalog["roi_board_03.png"]["panels"][5]["frame_id"], "roi_center_001",
        )
        self.assertEqual(
            catalog["roi_board_06.png"]["panels"][11]["frame_id"], "roi_right_003",
        )

    def test_eight_previously_reported_refs_are_actually_valid_no_changes(self):
        # The prior audit was wrong: all 8 labels reflect the real printed PNG headers.
        strings = [
            "board_03.png (PANEL #05, PANEL #06), roi_board_03.png (PANEL #05)",
            "board_03.png (PANEL #05), roi_board_03.png (PANEL #05)",
            "board_03.png (PANEL #05), roi_board_03.png (PANEL #05)",
            "board_07.png (PANEL #13), roi_board_03.png (PANEL #06)",
            "board_10.png (PANEL #20), board_11.png (PANEL #21), roi_board_04.png (PANEL #07)",
            "board_14.png (PANEL #27, PANEL #28), roi_board_06.png (PANEL #12)",
            "board_16.png (PANEL #31, PANEL #32), roi_board_05.png (PANEL #09)",
            "board_16.png (PANEL #31, PANEL #32), roi_board_06.png (PANEL #11)",
        ]
        data = {
            "scene_analyses": [
                {"evidence_ref": "roi_board_03.png (PANEL #05), roi_board_05.png (PANEL #09)"},
            ],
            "prompts_extracted": [{"evidence_ref": v} for v in strings[:6]],
            "ui_settings_extracted": [{"evidence_ref": v} for v in strings[6:]],
        }
        result, report = reconcile_analysis_evidence(data, manifest())
        self.assertEqual(result, data)
        self.assertTrue(report["all_refs_valid"])
        self.assertEqual(report["corrected_panel_refs"], 0)
        self.assertGreater(report["validated_panel_refs"], 8)

    def test_wrong_group_local_number_is_rejected(self):
        for bad in (
            "roi_board_03.png (PANEL #01)",  # Real header is #05.
            "roi_board_04.png (PANEL #03)",  # Real header is #07.
            "roi_board_05.png (PANEL #01)",  # Real header is #09.
            "roi_board_06.png (PANEL #03)",  # Real header is #11.
            "roi_board_07.png (PANEL #01)",
            "board_04.png (PANEL #99)",
            "roi_board_03.png",
            "roi_board_03.png (PANEL #foo)",
        ):
            with self.subTest(bad=bad), self.assertRaises(InvalidEvidenceReference):
                reconcile_analysis_evidence(
                    {"prompts_extracted": [{"evidence_ref": bad}]}, manifest(),
                )

    def test_missing_ref_fails_atomically_without_modifying_original(self):
        data = {"prompts_extracted": [
            {"evidence_ref": "roi_board_03.png (PANEL #05)"},
            {"evidence_ref": "roi_board_03.png (PANEL #01)"},
        ]}
        with self.assertRaises(InvalidEvidenceReference):
            reconcile_analysis_evidence(data, manifest())
        self.assertEqual(data["prompts_extracted"][0]["evidence_ref"],
                         "roi_board_03.png (PANEL #05)")

    def test_new_explicit_manifest_panel_indices_must_match_renderer(self):
        example = manifest()
        for board in example["boards"]:
            if board["board_id"] == "roi_board_03":
                board["panel_indices"] = [5, 6]
        c = build_panel_catalog(example)
        self.assertEqual(sorted(c["roi_board_03.png"]["panels"]), [5, 6])
        for board in example["boards"]:
            if board["board_id"] == "roi_board_03":
                board["panel_indices"] = [1, 2]
        with self.assertRaises(InvalidEvidenceReference):
            build_panel_catalog(example)

    def test_existing_direct_frame_reference_resolves_without_guessing(self):
        analysis = {"ui_settings_extracted": [{"evidence_ref": "frame_001"}]}
        result, details = reconcile_analysis_evidence(analysis, manifest())
        self.assertEqual(result, analysis)
        self.assertEqual(details["validated_panel_refs"], 1)
        with self.assertRaises(InvalidEvidenceReference):
            reconcile_analysis_evidence(
                {"ui_settings_extracted": [{"evidence_ref": "frame_999"}]},
                manifest(),
            )

    def test_legacy_windows_absolute_paths_work_cross_platform(self):
        fixture = manifest()
        fixture["boards"][0]["path"] = (
            r"C:\\kmj-ai-agent\\YouTube-AI-Research\\data\\runs\\boards\\board_01.png"
        )
        catalog = build_panel_catalog(fixture)
        self.assertIn("board_01.png", catalog)
        self.assertIn(1, catalog["board_01.png"]["panels"])

    def test_single_roi_mismatched_frame_suffix_is_not_a_panel_label(self):
        c = build_panel_catalog(manifest())
        self.assertEqual(c["roi_board_05.png"]["panels"][9]["frame_id"], "roi_right_001")
        self.assertNotIn(1, c["roi_board_05.png"]["panels"])


if __name__ == "__main__":
    unittest.main()
