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


## WP1 — context-enhanced transcription research (proposed scope, 2026-11-25)

The target is faithful, useful speech transcription, not perfect orthographic
verbatim, silence classification or non-speech acoustic analysis. A01 is a
historical positive-control replay (semantic nonce match; numeric spelling
differs; reconstructed TUI response; no timing coverage). A02 and A03 are
historical diagnostics outside the revised product scoring scope. Preserve all
existing private evidence, including the unsaved A03 evaluation inputs. A04/A05
and further silence/tone calls are deferred. Never reinterpret those historical
tests as broad accuracy or provider-ingress proof.

Audit baseline: main after PR #30; the existing manual_verbatim single-input
contract, immutable persistence and path guards, media probe, clipboard helper,
headless text JSON parser and batch budget/checkpoint code remain reusable. The
existing GUI probe stages native input but its submitted route does not establish
attachment-chip validity before Enter, and its file-writing response contract
differs from this tool-free transcription task. The Phase 5F worker is synthetic
only. PR #29's separate read_file byte and transport audit remains open and
unmodified; it is not a dependency of speech-quality research.

### Research batch entrypoint (not a production worker)

Run from the public checkout using the current Python environment:

    python -m ytresearch.research_batch probe

Only version/help are queried from AGY. Flags are capability hints, not proof
of native headless media input or response logging. There is no automatic
brain-directory scan, arbitrary CLI invocation, or model submission. A native
headless MP4 attachment path and reliable machine-readable response capture
are still unverified. No Windows GUI validation has been performed by CI.

Prepare a batch with an existing, explicitly approved private runtime directory
and a local MP4 relative to that directory (no URL acquisition or private archive
discovery). Optional reference is a UTF-8 text file relative to the same private
root. Reference kinds distinguish unverified text, caption proxy and an
operator-declared human-reviewed reference; the declaration is not independent
proof. Example placeholders only:

    python -m ytresearch.research_batch prepare --root D:\private-runtime --media clips\sample.mp4 --reference references\sample.txt --reference-kind CAPTION_PROXY

Preparation validates video/audio streams and decodes the clip, builds separate
media-only directories, hashes media and common prompt, snapshots the optional
reference outside every model working directory, and generates a content-addressed
batch plan. All six candidate models are included by default. To select a subset
use --models and the exact allowed IDs; --trial distinguishes deliberate repeats.
The root must exist outside Git. All real files, captures, plans and reports
remain private. No additional source assets are searched. This is a trusted local
operator boundary, not protection against a hostile concurrent filesystem actor.

No-submit Windows smoke, before live experiments:

    python -m ytresearch.research_batch prepare --root D:\private-runtime --media clips\sample.mp4 --models gemini-3.8-flash-medium --stage-only

The same mode can be run against an existing unsubmitted plan using the stage
subcommand with --root and --batch. Type READY only after the correct AGY
session is visibly ready. The script stages media and prompt, but the operator
must not submit; verify the chip/prompt, close AGY, and type STAGED. This
records an operator declaration, not a provider receipt. It consumes no
submission reservation. The script itself never presses Enter.

Optional guided Windows mode, only for media authorized for provider submission:

    python -m ytresearch.research_batch run --root D:\private-runtime --batch BATCH_ID --authorize-guided --max-calls 1

Or use prepare with --guided --max-calls 1 to combine the steps. Increase the
total attempt ceiling only with owner authorization (maximum six per batch).
Install optional GUI requirements using the existing setup script if needed.
The controller checks flags and dependencies, reserves a possible submission
before launching a uniquely identified terminal, asks the operator to verify
readiness, then stages MP4 and prompt using the existing clipboard/focus
helpers. The operator must visually verify the correct media chip and prompt,
submit exactly once, copy the response JSON, close AGY and type CAPTURE in the
controller. The controller never types Enter, approves trust dialogs, or retries.
The clipboard source is not provider-original bytes. A missing/invalid capture,
unsettled claim or unexpected open window stops the batch. A stale controller
lock is deliberately not automatically reclaimed. Review local evidence before
starting any new trial. Do not retry uncertain submissions merely because a
response was not captured.

    python -m ytresearch.research_batch report --root D:\private-runtime --batch BATCH_ID

Reports contain aggregate metrics/status, not transcript content. Raw captures
are immutable and hashed; invalid captures are retained. CER/WER measure
reference agreement, normalized CER removes whitespace/punctuation only.
Empty valid responses are scored as complete reference deletions when a
nonempty reference exists, not silently excluded from quality scoring.
No automatic numeric equivalence, semantic judgment, human verification or
quota estimate is fabricated. A nonempty model response is not a quality pass.
Attempt reservations are not verified billable requests. Wall-clock timing is
operator-inclusive and not provider latency. Provider ingress, OS isolation,
and quota accounting are unverified. No source or transcript is published.

Next: validate this guided path on Windows without submitting until attachment
readiness is verified; after owner-approved real sample and call budget, compare
six models on identical speech, reuse captures for context correction, select
clip lengths, and only then develop the production pipeline. A fully unattended
MP4 adapter remains a blocker; do not enable automatic submission based solely
on help flags or keyboard timing.
