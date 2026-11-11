# Public development contract

## Canonical repository

The sole target for **new code development and GitHub offline CI** is the public repository. The historical Private repository is read-only reference and private research archive. Do not add a private-repository checkout, access token, full-history mirror or external submodule to Public CI.

Migration remains staged in PR #1 pending the actual offline security/CI gates. License selection, binary redistribution analysis and organization-style branch protection are **not blockers** for this single-maintainer, no-third-party-distribution project. Public `main` becomes the canonical development code once this PR is merged.

## Python workflow (offline)

Use an isolated Python 3.12 environment (3.13 optional), then:

```sh
python -m pip install -r requirements-dev.txt
python -m tools.check_public_snapshot
python -m compileall -q ytresearch tests tools
python -m unittest discover -s tests -v
python -m ytresearch phase5e-validate
python -m ytresearch phase5d-stress --workers 12 --rounds 3
```

Windows and Ubuntu CI both must pass. Use `pull_request` checks with `contents: read`, isolated fixtures, no credentials and no actual model or YouTube calls.

## Data and branch discipline

- The historical source ledger contained **77 source entries**. Do not recreate actual identifiers or URLs from examples. Duplicated source entries can refer to one video: source-count and unique-video-count are distinct.
- Tests generate synthetic source entries in temporary directories. Never place real source data in `research/` or make it a tracked public directory.
- Publish only reviewed implementation code and documented invariants. Keep original model output, media and research decisions in their separate private archive.
- GitHub Actions do not deploy a private worker or authorize model usage. A passing synthetic check does not mean production approval.
- No automated merge to public `main` without successful security/CI checks and an explicit maintainer decision. GitHub branch protection remains recommended defense-in-depth, not a mandatory licensing or CI precondition for this personal project.

## Operational caveats

- The public `publish-batch` command is always disabled: see `docs/PUBLIC_RUNTIME_BOUNDARY.md`.
- Running some other live processing command manually can still call external tools; request separate authorization before any real use.
- Phase 5F needs a separate Draft PR, offline adapter injection, immutable staged artifacts and safe budget settlement tests before a real pilot.
