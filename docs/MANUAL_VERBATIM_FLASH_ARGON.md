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
