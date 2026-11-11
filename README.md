# YouTube AI Research

An offline-testable Python toolkit for structured YouTube source processing, multimodal evidence, research verification, and controlled batch-worker orchestration.

> **Migration draft.** This public repository contains code and synthetic fixtures only. Real source registries, transcripts, screenshots, model responses, private analysis, credentials and prior private Git history are intentionally excluded.

## Local development

Use Python 3.12 or 3.13 in an isolated environment. Install development dependencies with `python -m pip install -r requirements-dev.txt`.

Run deterministic offline checks (including Phase 5F synthetic queue-to-worker integration):

```sh
python -m compileall -q ytresearch tests tools
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p test_phase5f_offline.py -v
python -m ytresearch phase5e-validate
python -m ytresearch phase5d-stress --workers 12 --rounds 3
```

**These commands are designed for synthetic fixtures and are not permission to run production video ingestion or paid AI model calls.** CI runs entirely offline after installing dependencies.

## Data separation

- `research/`, `data/`, `cache/` and `sources.csv` hold private runtime material and are not included in the public source tree.
- Tests create disposable synthetic source registries and sample artifacts; do not commit real URLs, video IDs, transcripts or model output.
- Real model/video operations require separate authorization and a private runtime environment.
- The code is being migrated under a Draft PR. No production correctness claim is made by passing synthetic tests.

## Development process

Contributions go through pull requests and offline CI on Windows and Ubuntu. Avoid printing credentials, raw source URLs, or private research artifacts in logs or CI artifacts. See `docs/PUBLIC_MIGRATION.md`, `docs/PUBLIC_RUNTIME_BOUNDARY.md`, and `docs/research/LEGACY_PRIVATE_HANDOFF.md` for migration gates, executable safety boundaries and decisions recovered from the private history.

## License status (personal-use project)

This repository is public for the maintainer's own development and GitHub Actions use. **No MIT, Apache-2.0 or other open-source license has been granted**, and there is no root `LICENSE` file. Third-party reuse rights are not conferred merely by public visibility (GitHub's platform terms still apply). The maintainer has no current plans for third-party collaboration, binary distribution or selling this code.

Dependency notices and potential future redistribution obligations are documented in `docs/LICENSING_AND_REDISTRIBUTION_AUDIT.md` for reference only. **License selection and binary redistribution approval are not prerequisites for personal development or PR #1 merge.** Private video/transcript output remains excluded.
