"""Offline policy unit tests; contains no real URLs, credentials or corpus."""
import unittest

from tools.check_public_snapshot import scan_record


class PublicSnapshotAuditTests(unittest.TestCase):
    def test_clean_synthetic_source_fixture_is_allowed(self):
        self.assertEqual(
            scan_record(
                "tests/fixture.py",
                'url = "https://youtube.com/watch?v=Tst00000001"\n',
            ),
            [],
        )

    def test_private_directory_is_rejected(self):
        self.assertIn(
            "PRIVATE_CORPUS_PATH",
            scan_record("research/index.json", '{"source_count":77}'),
        )

    def test_unreviewed_video_url_is_rejected(self):
        example_id = "ABC" + "12345678"
        bad = f'url = "https://youtube.com/watch?v={example_id}"'
        self.assertIn(
            "REAL_OR_UNREVIEWED_VIDEO_URL",
            scan_record("tests/fixture.py", bad),
        )

    def test_token_like_literal_is_rejected(self):
        token = ("AI" + "za") + "0" * 32
        self.assertIn(
            "TOKEN_LIKE_LITERAL", scan_record("tests/fixture.py", token)
        )

    def test_public_publisher_guard_cannot_be_removed(self):
        self.assertIn(
            "RESEARCH_PUBLICATION_GATE_MISSING",
            scan_record("ytresearch/batch.py", "def publish_batch(): pass"),
        )
        self.assertIn(
            "RESEARCH_PUBLICATION_GATE_MISSING",
            scan_record("ytresearch/__main__.py", "def main(): return 0"),
        )

    def test_nonexample_email_is_rejected(self):
        email = "operator" + "@" + "nonexample.invalid"
        self.assertIn("NON_EXAMPLE_EMAIL", scan_record("tests/fixture.py", email))


if __name__ == "__main__":
    unittest.main()
