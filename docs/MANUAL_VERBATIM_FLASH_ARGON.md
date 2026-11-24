# Manual source-faithful transcription — Gemini Flash + Argon

## Scope

One video at a time. Preserve every audible word possible in the original
language, timestamps, corrections, repetitions and nontechnical speech.
Screenshots are saved only for owner-selected important screens. No batch,
scheduler, unattended model invocation or 19-section report requirement.

The old subtitle-only Python collector could omit spoken content. A JSON3
subtitle file is not proof of complete audio coverage. AGY model-generated
text must NEVER replace the original caption bytes. Captions, Flash drafts
and Argon corrections remain three distinct source layers.

Interactive AGY supports attaching video files via its media paste workflow.
Headless agy -p does NOT document a native video attachment flag. A model
merely reading a filename, screenshot or JSON3 does not prove audio was heard.
If audio was not available to AGY, mark AUDIO_NOT_OBSERVED and do not invent
missing words. Source-faithful transcription requires listening to the actual
audiovisual file; model output is review-required evidence only.

## Owner-run private workflow

Use Public checkout Python 3.12 venv, but make the private Runtime the root.

    $python = Join-Path (Get-Location).Path ".venv\Scripts\python.exe"
    $runtime = "C:\your-private-runtime"
    $manifest = Join-Path $runtime "data\pilot_candidate_1_manifest.json"

Prepare a complete literal JSON3 caption rendering and Flash/Argon prompts:

    & $python -m ytresearch --root $runtime verbatim-manual prepare --manifest $manifest

Current pilot video files have no embedded audio. Remux the existing 720p
AV1 file with the existing AAC file to a private MP4 without transcoding:

    & $python -m ytresearch --root $runtime verbatim-manual playback --manifest $manifest

Check that the result actually plays both visible frames and audible speech.
If the remux fails, do not invoke AGY; collect the failure details locally.

In interactive agy select Gemini 3.8 Flash (Medium), attach the MP4 as VIDEO
using the supported clipboard-media flow, and paste FLASH_INSTRUCTIONS.txt
with the literal transcript for comparison. Process the entire video in
manageable consecutive time windows; record coverage and unrecognized words.
Save the entire response as a UTF-8 text file in the private Runtime.

In a separate interactive session select Gemini 4 Argon (High), attach the
SAME audiovisual file, provide the complete Flash draft plus the original
JSON3 and paste ARGON_INSTRUCTIONS.txt. Ask for verbatim corrections and
missing speech, NOT a summary. Preserve the complete response separately.

For a visually important command, prompt, setting, diagram or error, save
a high-resolution frame at a selected actual video timestamp:

    & $python -m ytresearch --root $runtime verbatim-manual screen --manifest $manifest --second 120.5

Import the two saved model replies as immutable, unverified evidence:

    & $python -m ytresearch --root $runtime verbatim-manual import --manifest $manifest --stage flash --file $flashFile
    & $python -m ytresearch --root $runtime verbatim-manual import --manifest $manifest --stage argon --file $argonFile
    & $python -m ytresearch --root $runtime verbatim-manual export --manifest $manifest

The resulting file is private:
data/verbatim/<video-id>/verbatim_timeline_REVIEW_REQUIRED.md

It includes all caption-text events (without text normalization), the
unedited Flash and Argon responses and selected screenshot references.
The entire raw original JSON3 remains separately stored, including non-text
events. No semantic verification, transcript completeness or human approval
is inferred by importing a model reply.

## Safety and operating limits

Every operation revalidates all four original input SHA-256 values from the
private pilot manifest. Existing outputs are never silently overwritten.
No AGY, YouTube or API calls occur through these new Python commands.
Model use is explicitly interactive and manually controlled by the owner.
Review quota and account state in interactive AGY before each model request.
Private transcripts, video IDs, images, account information and research
artifacts must never be committed to the Public repository.

Do not claim exact spoken-word fidelity solely because the caption timeline
is complete or two models agree. Verify questionable intervals by listening
to source audio; preserve [unintelligible] markers rather than guessing.


## H.264/AAC compatibility for Windows and interactive AGY

The existing playback_with_audio.mp4 preserves the source AV1 codec and
may show an AV1 unsupported error in Windows Media Player. PyAV may still
decode it successfully. Microsoft AV1 Video Extension is an option for
local playback, but does not guarantee AGY media compatibility.

A separate H.264 video with the original AAC bitstream is more widely
compatible. Install FFmpeg if it is absent:

    winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements

Open a fresh PowerShell session so ffmpeg can be found on PATH, then:

    & $python -m ytresearch --root $runtime verbatim-manual playback-compatible --manifest $manifest

Output: data/verbatim/<video-id>/playback_h264_aac.mp4.
The 720p video is re-encoded with libx264 CRF 19 and yuv420p; the AAC
audio stream is copied without re-encoding. The original AV1, 4K,
AAC, JSON3, and previously remuxed MP4 are never modified. A temporary
output is checked for codec, duration, dimensions and decodable video
and audio before publishing without overwrite.

This is not a lossless conversion of video frames; preserve the 4K AV1
original for exact-resolution screenshot evidence. Play and hear the
result on the local system before attaching to AGY; this does not yet
prove that the AGY model processed the audio stream.

## Single local video manifest contract (verbatim_single_v1)

Phase 1 separates the input contract for authorized local single-video transcription
without requiring registration in the legacy 77-source ledger:

- Set `"schema_version": "verbatim_single_v1"` in the manifest JSON.
- `video_id`: 11-character identifier (`[A-Za-z0-9_-]{11}`).
- Registry lookup (`read_source_ledger`) is completely bypassed for this schema.
- Legacy manifests (without `schema_version`) continue to enforce 77-source ledger registration and require all 4 asset roles.

### Asset roles in verbatim_single_v1

| Role | Status | Description | Fallback behavior |
| --- | --- | --- | --- |
| `primary_video` | Required | Main video container | None (fails if missing) |
| `highres_video` | Optional | High-resolution video for screenshots | Falls back to `primary_video` |
| `audio` | Optional | Separate audio track (e.g. AAC, M4A) | Uses audio stream inside `primary_video` |
| `subtitle_json3` | Optional | YouTube JSON3 subtitle | Pure audio verbatim mode (captions marked unsupplied) |

Every specified asset must satisfy:
- Regular non-symlink file under the runtime directory
- Exact byte length match (`size_bytes`)
- Exact SHA-256 hex digest match (`sha256`)
- Immutable output creation: existing files cannot be overwritten or altered.

### Phase 1B input contract, isolation and verification enhancements

1. **Deterministic input digest isolation (`input_digest`)**:
   - For `verbatim_single_v1`, outputs are written to `data/verbatim/<video_id>/<input_digest>/`.
   - `input_digest` is the deterministic SHA-256 hash of the canonical JSON containing `schema_version`, `video_id`, and sorted asset roles with verified SHA-256 digests and file sizes.
   - An immutable `execution_manifest.json` is saved in the output directory, preventing collision or artifact reuse across different inputs for the same video ID. Legacy outputs remain at `data/verbatim/<video_id>/`.

2. **Caption requirement contract & original JSON3 integrity**:
   - `export()` determines caption requirement based on the input manifest rather than local file presence.
   - Legacy inputs and new inputs specifying `subtitle_json3` require both `original_captions.json3` and `captions_unedited.md`.
   - Archived `original_captions.json3` SHA-256 must match the verified input manifest, rejecting simultaneous tampering of JSON3 and rendered Markdown.
   - Pure audio verbatim export without captions is permitted only for single inputs where `subtitle_json3` is omitted.

3. **Strict path and boundary safety**:
   - Asset paths and output paths are strictly validated against the approved runtime root.
   - Leaf symlinks, parent directory symlinks, NTFS directory junctions, and directory traversal attempts (`..`) are rejected fail-closed before any processing.

4. **Media and stream validation**:
   - Input containers must have valid streams: `primary_video` must contain a decodable video stream, and audio (embedded or separate) must contain an AAC stream.
   - Non-AAC codecs, missing streams, or corrupted containers fail explicitly with descriptive errors (`STREAM_MISSING`, `AUDIO_CODEC_UNSUPPORTED`, `MEDIA_CORRUPTED`).

5. **Schema determination & conflict prevention**:
   - Manifests specifying conflicting `schema_version` and `contract` or unsupported future schemas fail immediately (`SCHEMA_CONFLICT`, `SCHEMA_UNSUPPORTED`).
   - Normal legacy manifests without schema keys remain fully compatible with 77-source ledger verification.

6. **Prompt contract adaptation**:
   - When `subtitle_json3` is omitted, instructions generated for Flash and Argon explicitly note that no subtitles are supplied, avoiding instructions that demand nonexistent JSON3.

### Phase 1C audit verification and residual defect remediations

1. **Output path and screens directory safety (G01)**:
   - `screen()` validates `screens/` intermediate directory and target files against symlinks, NTFS directory junctions, and path escape before writing via `create_immutable()`.
   - `export()` verifies screens directory and child PNG files, refusing to enumerate symlinked or external screen captures as evidence.
   - Intermediate partial files and destination outputs across `playback`, `playback_compatible`, and `screen` are verified within approved runtime bounds.

2. **Common execution manifest and review binding (G02)**:
   - `execution_manifest.json` is verified across all downstream operations against schema version, video ID, input digest, and verified asset SHA-256 and size.
   - When output artifacts exist without an execution manifest, or when the manifest is tampered, automatic reuse is strictly forbidden (`EXECUTION_MANIFEST_MISSING`, `EXECUTION_MANIFEST_TAMPERED`).
   - Manual review imports create immutable review receipts (`<stage>_review_manifest.json`) binding the draft response hash, input digest, and video ID. Copying review drafts across runs is rejected (`REVIEW_REUSE_FORBIDDEN`).

3. **Public Git and runtime separation (G03)**:
   - Running verbatim commands with a runtime root located inside a Git checkout or worktree is rejected before any files are opened or written (`PUBLIC_GIT_RUNTIME_FORBIDDEN`).

4. **Reliable manifest path resolution (G04)**:
   - Relative manifest paths are resolved deterministically against the runtime root, independent of the current working directory, and verified prior to any output file generation.

5. **Strict schema validation and traversal prevention (G05)**:
   - Unsupported explicit schema values (empty string, integer, boolean, null, conflicting schema/contract) are rejected fail-closed (`SCHEMA_UNSUPPORTED`, `SCHEMA_CONFLICT`).
   - Directory traversal (`..`) in asset paths, manifest paths, or review files is rejected (`PATH_TRAVERSAL_FORBIDDEN`).

### Phase 1C-Final hardening

1. **Pre-read boundary validation (D-01)**:
   - Manifest path, Git worktree boundary, `data/` containment, and all parent symlink/junction checks complete before any manifest JSON file is read, and the validated context is reused across downstream stages without re-reading the manifest.
2. **Unified top-level Runtime root boundary (D-02)**:
   - All input and output operations (`prepare`, `playback`, `playback_compatible`, `screen`, `import_review`, `export`) enforce the approved Runtime root as the single trust boundary and reject symlinks or junctions on `data/`, `data/verbatim/`, `screens/`, and leaf targets.
3. **Ordered execution manifest and idempotent retry (D-04, D-06)**:
   - `prepare()` validates inputs and captions before atomically publishing `execution_manifest.json` via `os.link`, then writes immutable artifacts; `playback()` and `playback_compatible()` validate media streams and AAC codec before writing `execution_manifest.json`.
4. **Fail-closed JSON object validation and CI FFmpeg check (D-03, D-05)**:
   - Non-object JSON payloads for manifests, captions, execution manifests, and review receipts fail closed with `VerbatimError`.
   - Offline CI workflows explicitly verify `ffmpeg` availability with `libx264` and `aac` encoders on Ubuntu and Windows.
