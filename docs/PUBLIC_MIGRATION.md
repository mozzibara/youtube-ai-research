# Public migration and validation policy

This repository is a **clean-room public source snapshot**, not a fork, mirror, or transfer of the original private research history. It is currently under migration review. No private source registry or media artifacts are included.

## Permitted materials

- Python implementation in `ytresearch/` and `tools/`
- Synthetic and isolated regression tests in `tests/`
- Generic development configuration, dependency requirements and agent definitions
- This policy, public README, GitHub Actions CI configuration

## Excluded materials

The actual source registry, playlist, video IDs, subtitles, transcripts, media, screenshots, research timeline, internal review, model provider output, authentication material, private execution logs and original Git commit history.

The tests use synthetic 11-character video identifiers with `Tst`/`Ytx` prefixes. These are test values only. **Never restore production video IDs or real transcript excerpts to these fixtures.**

## Acceptance gates

1. Audit tracked Git files with `python -m tools.check_public_snapshot`.
2. Compile Python code and run all offline unit tests.
3. Pass `phase5e-validate` (16 synthetic scenarios).
4. Pass `phase5d-stress --workers 12 --rounds 3` with no violations.
5. Require successful Windows and Ubuntu GitHub Actions checks.
6. Verify public `publish-batch` is blocked at the CLI and Python API even with `--commit --push --acknowledge-rights`.
7. Confirm missing CLI prerequisites preserve their previous stage and fail with a nonzero exit code.
8. Review historical evidence-panel and PNG rendering regression tests without importing old production modules.
9. Before merging, pass the public-file security scan and cross-platform offline CI, and review the PR. Licensing choice, binary redistribution audit and administrator-managed branch rules are *not* mandatory gates for personal-only development. Do not automatically deploy or publish private data.

Passing synthetic CI does not authorize real YouTube downloading, paid AGY calls, output publication or production use. Real processing remains subject to separate authorization in a private execution environment.

**License:** no root software license is currently granted; public visibility alone does not grant third-party reuse rights. MIT/Apache choice is optional unless the maintainer later wants to license the code. Third-party library terms must be followed, and bundled redistribution would require its own review.
