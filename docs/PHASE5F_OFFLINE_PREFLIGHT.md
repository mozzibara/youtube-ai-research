# Phase 5F.5 — isolated offline preflight and owner handoff

## Command

```sh
python -m ytresearch phase5f-validate
```

This command is **offline-only and standalone**. It deliberately ignores `--root` and creates a new isolated 77-source synthetic fixture in a temporary directory for each scenario. It does not access real videos, source URLs, local credentials, user media or paid model providers.

The JSON report includes only fixed scenario IDs, statuses, error categories and numeric aggregate counts. No lease token, source URL, media bytes, original model output, local path or real research artifact is returned. Success exits 0; any failed scenario exits 2.

## Independent scenarios

| ID | Gate |
| --- | --- |
| F01 | Successful synthetic queue claim → 9-stage file chain → immutable receipt → settled budget |
| F02 | Synthetic provider 429 → global manual pause and an unsettled reservation |
| F03 | Altered fake media rejected before claim and budget reservation |
| F04 | Crash before SQLite receipt commit leaves orphan objects, not completed work |
| F05 | Crash after receipt commit resumes once with the original owner/lease proof |
| F06 | Corrupted content-addressed object blocks receipt verification |
| F07 | Multiple simultaneous writes for identical content do not overwrite one another |
| F08 (conditional) | Report unauthorized socket/subprocess/AGY/YouTube attempts as a failure |

No scenario performs real provider delivery, proves exactly-once external operations, or grants production approval. A zero-call report means the instrumented Python guard observed no such calls in these scenarios; it is not an OS sandbox.

## Acceptance

Run from either Windows or Ubuntu:

```sh
python -m unittest discover -s tests -v
python -m ytresearch phase5e-validate
python -m ytresearch phase5f-validate
python -m ytresearch phase5d-stress --workers 12 --rounds 3
```

All four commands are included in standard Public GitHub CI, along with the tracked-file audit. No manually executed local commands, credentials, YouTube videos or live API calls are required for **Phase 5F.5**.

## Remaining work needing owner authorization

Moving beyond synthetic fixtures requires (1) selecting an authorized private runtime and source, (2) inspecting actual local media/model tool availability, (3) reviewing provider credentials, quotas and access boundaries, and (4) explicitly authorizing an initial limited real run. The existing Public code must not automatically upload videos or publish transcripts. The original Private repository remains an archive; do not use it as a Public CI dependency.

This document is a stop-point for offline-only development, **not** an instruction to start actual media processing.
