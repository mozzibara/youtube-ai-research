"""Adapted non-media legacy renderer assertions. Never reads private videos."""
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from ytresearch.evidence_refs import (
    InvalidEvidenceReference, build_panel_catalog, reconcile_analysis_evidence,
)
from ytresearch.multimodal import build_visual_evidence_boards


class LegacyRenderedPanelTests(unittest.TestCase):
    def test_rendered_png_board_metadata_uses_global_roi_panel_indices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = []
            for index in range(6):
                image = root / f"synthetic_{index:03d}.png"
                Image.new("RGB", (32, 32), (index * 20, 20, 20)).save(image)
                frames.append({
                    "frame_id": f"roi_center_{index + 1:03d}",
                    "path": str(image),
                    "actual_sec": float(index * 10),
                    "source_width": 32,
                    "source_height": 32,
                    "width": 32,
                    "height": 32,
                })
            boards = build_visual_evidence_boards(
                frames, root / "boards", panels_per_board=2, prefix="roi_board",
            )
            self.assertEqual([b["panel_indices"] for b in boards],
                             [[1, 2], [3, 4], [5, 6]])
            for board in boards:
                self.assertTrue(Path(board["path"]).is_file())
            catalog = build_panel_catalog({"boards": boards})
            self.assertEqual(
                catalog["roi_board_03.png"]["panels"][5]["frame_id"],
                "roi_center_005",
            )

    def test_multiple_boards_and_malformed_panel_annotations_fail_closed(self):
        def board(n):
            return {
                "board_id": f"roi_board_{n:02d}",
                "path": f"roi_board_{n:02d}.png",
                "panel_frame_ids": [
                    f"roi_center_{2*n-1:03d}", f"roi_center_{2*n:03d}",
                ],
                "panel_indices": [2*n-1, 2*n],
            }
        fixture = {"boards": [board(i) for i in range(1, 4)]}
        value = "roi_board_02.png (PANEL #03, PANEL #04), roi_board_03.png (PANEL #05)"
        data = {"prompts_extracted": [{"evidence_ref": value}]}
        result, report = reconcile_analysis_evidence(data, fixture)
        self.assertEqual(result, data)
        self.assertEqual(report["validated_panel_refs"], 3)
        for bad in ("roi_board_03.png (PANEL #01)", "roi_board_03.png",
                    "roi_board_03.png (PANEL #05 and #06)", "PANEL #05"):
            with self.subTest(bad=bad), self.assertRaises(InvalidEvidenceReference):
                reconcile_analysis_evidence(
                    {"prompts_extracted": [{"evidence_ref": bad}]}, fixture,
                )


if __name__ == "__main__":
    unittest.main()
