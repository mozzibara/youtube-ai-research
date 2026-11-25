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

### WP1 paired response capture pilot

After the no-submit Windows smoke succeeds, use the same unsubmitted
single-model synthetic batch for one explicitly authorized call:

    python -m ytresearch.research_batch run --root D:\private-runtime --batch BATCH_ID --authorize-guided --max-calls 1 --probe-log

This opt-in mode adds a unique, private --log-file destination to the same TUI
launcher. It also snapshots only immediate directory names under the documented
AGY brain directory before launch, and after closure reads at most the direct
transcript_full.jsonl in a unique newly created directory. No existing session
file is opened, no recursive search is performed, and multiple new directories
are ambiguous. A session candidate requires exactly one matching prompt turn
and a completed, schema-valid MODEL planner response without recorded tool calls.
A dedicated CLI log candidate requires a unique terminal SUCCESS result event,
not debug text or partial deltas. Preserve the actual clipboard capture first.
Compare the candidates with the clipboard JSON; keep log snapshots and results
only inside the private batch. A matching pair is evidence for this session,
not proof of automated completion detection, provider ingress or future stability.
If neither candidate is available, the pilot still preserves the manual capture;
do not resubmit just to obtain logs. The operator must copy the response before
closing AGY and type CAPTURE manually. No automatic Enter or retry.

### Interrupted paired capture recovery

If CAPTURED was printed but the terminal closure check blocked the log probe,
close only that experiment window. Do not send another prompt. Run:

    python -m ytresearch.research_batch recover-log --root D:\private-runtime --batch BATCH_ID --confirm-closed

Recovery checks the existing single-job claim and receipt, response hash, and
schema. It inspects only a unique dedicated agy-<32 hex>.log in that job folder,
or reuses an existing completed probe. No AGY, GUI, clipboard or new claims.
Missing/ambiguous logs are reported without guessing. The historical before
session directory snapshot was held in memory, so it cannot be recovered from
this stopped process; no old brain session is scanned. A new guided call now
allows a short window-teardown grace period and a close confirmation before
stopping with its already preserved capture. A captured UNAVAILABLE response
is a speech-quality failure, not proof of successful transcription.

### Session-first capture follow-up

The first local dedicated CLI log was text diagnostics and contained no
recognizable matching response. Prefer the session trace; do not expand the
CLI debug parser on this evidence. The prelaunch directory inventory is now
persisted privately. If interrupted, recover using recover-session with
--confirm-closed. Historical recovery without an inventory follows only a
unique explicit UUID-valued session/conversation/cascade ID in the verified
dedicated log snapshot; it never searches old transcript contents or guesses
from timestamps. If no exact identifier exists, it reads zero session logs.

    python -m ytresearch.research_batch recover-session --root D:\private-runtime --batch BATCH_ID --confirm-closed

For an explicitly approved new single-call paired pilot without --log-file:

    python -m ytresearch.research_batch prepare --root D:\private-runtime --media clips\sample.mp4 --models gemini-3.8-flash-medium --trial session-pilot-1 --guided --max-calls 1 --probe-session

The usual READY, manual single submit, clipboard copy, close and CAPTURE
remain for this pilot only. Session comparison runs after closure and can be
recovered without resubmission. Only allowlisted schema counts are reported;
raw trace is retained privately only after prompt/response correlation.
A paired match does not establish autonomous completion or speech accuracy.

### Nested session traces and UTF-8 evaluation

Local AGY 1.5.16 inventory revealed transcript.jsonl and transcript_full.jsonl
two levels below the uniquely identified session directory. Recovery now
searches only that correlated directory, at most depth two and 500 entries,
for the two exact known filenames. It reads at most two bounded traces,
rejects reparse points and conflicting candidate contents, and requires
strict prompt/final-response correlation with the preserved clipboard JSON.
Identical file length is not identity; hashes and parsed response agreement
are reported. Structural counts are private-safe and do not expose trace
text. Existing direct-file recovery remains supported. No new model call is
required. Use recover-session with the existing batch and --confirm-closed.

Reference evaluation must read approved UTF-8 files directly, not non-ASCII
Python literals piped through Windows PowerShell's default native encoding.
A reference with no normalized characters is rejected instead of producing
a misleading score. Earlier diagnostic scores with an empty normalized
reference are invalid, not an ASR failure. Preserve the old diagnostic
artifact. The historical synthetic reference remains a proxy, not human
listening evidence.
