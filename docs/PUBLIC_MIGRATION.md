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
6. Keep the migration PR in Draft until all safety and licensing reviews are complete. Do not merge automatically.

Passing synthetic CI does not authorize real YouTube downloading, paid AGY calls, output publication or production use. Real processing remains subject to separate authorization in a private execution environment.

**License:** licensing and third-party redistribution checks must be completed before announcing an open-source grant. Public visibility alone is not a license.
