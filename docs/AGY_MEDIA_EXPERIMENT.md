# AGY native media attachment experiment (Windows)

This is experimental, **source-only**, and executes locally on an interactive Windows desktop.
It uses the existing Git checkout, not a new clone. Keep MP4, WAV, transcripts, screenshots, browser profiles, tokens, and runtime logs **outside Git**. The current project's public/private separation rules continue to apply.

## Setup in the existing checkout

From PowerShell in the project root with the current venv:

```powershell
git remote -v
git status --short
.\scripts\setup-agy-media.ps1
```

AGY CLI, Windows Terminal and FFmpeg are external programs. The setup script checks their availability but does not install or change their authentication or security settings.

For updates, first review any uncommitted work with `git status --short`, then use `git pull --ff-only origin main`. On a new machine only, clone once; do not clone again into this checkout.

## Existing local SBS 30-second trial

Use your existing runtime directory (with `agy_clean_workspace/clip_00_30.mp4` already prepared). Do not copy its real video or references into this repo's tracked files.

```powershell
$run = "D:\local-runtime\agy-experiments\sample-30s"
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio -DryRun

# Stages WAV and prompt in AGY, but does not submit a model request:
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio

# Close the staging window. After checking the correct media was attached,
# explicitly authorize the model call:
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio -Submit
```

### Inspect the actual installed AGY CLI without a model request

The public source now includes an independent local capability probe.
Run **only** the following commands in the existing Python venv / Public checkout:

```powershell
git pull --ff-only origin feature/agy-audio-byte-receipt-audit
python .\tools\agy_audio_capability_probe.py
```

The probe locates the first `agy` executable in `PATH` and runs
**only** `agy --version` and `agy --help`, each with an eight-second
timeout and stdin closed, in a disposable working directory. It sets
`AGY_CLI_DISABLE_AUTO_UPDATE=true`, never uses `-p`, launches an
interactive conversation, sends media, or requests any model inference.
It does not read AGY credentials, configuration, cached transcripts or
private experiments. Because it starts the CLI binary, its own internal
startup telemetry cannot be absolutely ruled out; model request
submission is not part of this probe's commands.

The JSON report contains only a parsed semantic version, fixed-status
labels and **allowlisted** option names; it does not include raw stdout,
stderr, local executable paths, environment variable values or user
files. `audio_attachment_flags` records only explicitly declared options
(e.g. `--audio-file`); a word like "audio" in explanatory text or an
unrelated `--input-format` option cannot constitute an attachment flag.

Interpretation of `assessment`:

- `DOCUMENTED_AUDIO_FORMAT_EXPECTED_ENTRYPOINT_UNVERIFIED` means the
  installed version is at/above the published v1.1.18 format-recognition
  change, but CLI help did **not** establish a native audio entry point.
- `AUDIO_NAMED_FLAG_CANDIDATE_ATTACHMENT_NOT_VERIFIED` indicates a
  recognized audio-named flag is shown in the installed help; it does
  not confirm MP3 attachment, payload or provider receipt.
- `OLDER_BUILD_AUDIO_SUPPORT_UNKNOWN` is a version older than the
  documented audio-recognition note; it is **not** a proven lack of
  audio capability.
- `HELP_NOT_AVAILABLE_ATTACHMENT_UNKNOWN`,
  `VERSION_UNKNOWN_ATTACHMENT_UNVERIFIED`,
  `INSTALLED_CLI_UNAVAILABLE` or `UNSUPPORTED_CLI_LAUNCHER` leave
  capability unknown and must not trigger an automatic update.

Do not run `agy update`, `agy -p`, a TUI media paste, or a new MP3
trial as part of this offline capability gate. Future **model invocations**
must separately be authorized and include
`--dangerously-skip-permissions` as established for AGY requests.

### Installed Windows CLI capability inspection — observed result

The user's local no-model diagnostic for **AGY CLI v1.5.10** returned:

- `version_status=OK` and `help_status=OK`
- `audio_attachment_flags=[]` and `help_audio_keyword_seen=false`
- `assessment=DOCUMENTED_AUDIO_FORMAT_EXPECTED_ENTRYPOINT_UNVERIFIED`
- `model_called=false`, `prompt_submitted=false`,
  `native_audio_attachment_verified=false`,
  `provider_audio_ingress_verified=false`

**Determination:** Installed AGY CLI 1.5.10 is newer than the
published v1.1.18 audio-format recognition change, but `--help`
exposes **no audio-named attachment flag** and no audio keyword.
These observations neither establish an audio attachment entrypoint
nor refute audio support inside the interactive CLI. They do not
invalidate the earlier Windows 1.5.9 audio clipboard failure, and
they must not be converted into a successful model receipt.

The official CLI reference enumerates `--print`, `--input-format`,
`--sandbox`, etc., but does not describe a documented standalone
`--audio-file` CLI flag:
https://antigravity.google/docs/cli/reference/

The latest CLI prompting guide describes clipboard media as **images
and videos**, while the CLI release notes separately state support for
recognizing MP3 and other audio formats. The **Antigravity IDE**
changelog's file-picker / drag-and-drop audio support must not be
treated as an independently verified AGY **CLI** input feature.

The no-model `--version`/`--help` capability gate is now complete.
Do not repeat it on the same binary without a version change.
If native attachment verification is still required, the next
evidence boundary is a **single operator-controlled, no-Enter TUI
staging attempt** using disposable non-sensitive media in a temporary
workspace. It must record only whether a typed audio attachment
indicator appears, distinguish a media chip from plain inserted path
text, and exit without submitting a prompt. No automated key
presses, live AGY requests, private MP3 upload or provider-ingress
claims are authorized by this record.

### Scope of the AGY audio attachment support claim

As documented in the official Antigravity CLI v1.1.18 changelog
(2026-08-22), the CLI recognizes common audio attachment formats such as
`.mp3`, `.wav`, `.m4a`, `.aac`, `.flac` and `.opus`:
https://antigravity.google/docs/changelog?tab=cli

The official CLI prompt guide's **clipboard** media section, however,
currently lists images and videos, not audio:
https://antigravity.google/docs/cli/prompting

Therefore **audio format recognition must not be mistaken for support
of the tested Windows `CF_HDROP` + Alt+V clipboard transport**. The
existing audio clipboard failure remains valid for that specific local
transport and test; it is not a general proof that no AGY audio
attachment entry point exists. A future native-path experiment must
verify an independently supported entry point first, keep media inside
private runtime, and never submit an AGY model call before explicit
authorization. See `docs/AGY_BLIND_AUDIO.md` for the five-case archive
closure and why `stream-json` is not a binary receipt.

### Critical finding: audio clipboard paste is not supported by this CLI path

Observed on Windows AGY CLI 1.5.9: both WAV and MP3 passed the local
`CF_HDROP` clipboard registration check, but the CLI showed
`No image or video found in the clipboard`. The CLI's documented paste
formats cover images and videos, not direct audio clipboard pastes.
A native MP4 media chip appeared on an earlier attempt, but the model
reported that it could not access its audio track.

For this reason, **`-MediaType audio -Transport clipboard -Submit` is
blocked before launching AGY**, rather than spending tokens on another
predictable `NO_AUDIO_ACCESS`. The old `-MediaType audio` default
attachment-probe remains usable solely for diagnostic reproduction.

### Separate experimental path: AGY read_file tool

The CLI documentation describes `@filename` as sending the file path
to the agent, **not attaching its bytes**. As a separate hypothesis,
the tool-use route may allow the agent's `read_file` operation to
return audio to the model. This route is unverified on the local
Windows environment; it is **not** a native attachment and its result
must be treated as a model self-report until the tool trajectory and
original audio have been independently examined.

```powershell
$run = "D:\local-runtime\agy-experiments\sample-30s"
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio -AudioFormat mp3 -Transport file-read -DryRun
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio -AudioFormat mp3 -Transport file-read
# Close the staging window, review the staged text, then explicitly opt in:
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio -AudioFormat mp3 -Transport file-read -Submit
```

Tool-read output (if any) is isolated at
`flash_audio_file_read_mp3.json`, and status is
`automation_status_audio_file_read_mp3.json`. If a
`read_file` operation returns metadata rather than audio, the only
correct outcome is `NO_AUDIO_ACCESS`. Do not turn this output into a
ground-truth transcription without checking actual audio access.
Only run `-Submit` after the preceding non-submitting probe.

### WAV versus MP3 attachment diagnostic

A real Windows run reported `NO_AUDIO_ACCESS` and `attached_audio_seen=false` with a WAV clipboard file drop. **This does not prove the model cannot process audio:** the AGY TUI may simply have failed to attach that file. The `--attachment-probe` mode ends before submitting to the model, so use it first. Check the AGY prompt for a real `audio/*` media indicator; successful `CF_HDROP` registration is not sufficient.

To compare a compressed, standard audio format without overwriting the WAV failure record:

```powershell
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio -AudioFormat mp3 -DryRun
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio -AudioFormat mp3
# Do NOT use -Submit if AGY does not display a native audio attachment.
# After verifying the attachment in the interactive AGY window, close it and:
.\scripts\run-agy-media.ps1 -Run $run -MediaType audio -AudioFormat mp3 -Submit
```

MP3 is generated locally from the exact same prepared MP4; its model output is stored in `flash_audio_mp3.json` (not `flash_audio.json`) and the attempt status in `automation_status_audio_mp3.json`. Neither WAV nor MP3 has yet been proven to reach the Gemini model in this Windows AGY route. The source-only code's `--submit` mode cannot independently validate the binary transport; use it only when the native audio attachment is visibly present.

**Do not use `@clip_00_30.wav` to claim native multimodal transport.** AGY's `@` file mention passes a file path for an agent to read; it is a different transport contract from native audio media attachment.

The default mode is audio-only. The script derives a mono 16kHz PCM WAV from the prepared MP4 and validates length and codec. Use `-MediaType video` only when specifically testing MP4, because a previous native MP4 run reported `NO_AUDIO_ACCESS`.

To avoid stale success attribution, the runner will **not overwrite** `flash_audio.json` or `flash_mp4.json`. Archive earlier results before conducting a new run.

## Safety and interpretation

- This is an unattended GUI keystroke experiment **only after the operator has explicitly selected `-Submit`**. The `-DryRun` and staging-only modes do not send a model request.
- AGY is launched with the established `--dangerously-skip-permissions --sandbox` flags. This removes normal approval prompts and should be used only in an isolated, trusted workspace.
- An initial AGY folder-trust dialog may require **manual review**. The script does **not** automatically approve trust prompts.
- Clipboard `CF_HDROP` success and screenshot evidence do not prove that audio bytes reached Gemini. A model's self-reported audio access is likewise not independent proof.
- Error verdicts (`NO_AUDIO_ACCESS`, `NO_AUDIO_ATTACHED`, `UNSUPPORTED_AUDIO_FORMAT`) are failures, not transcripts. A successful model JSON must also contain nonempty timestamped speech segments.
- For an actual quality score, compare transcript to the original audio; reference-caption similarity is **not** CER/WER or ground-truth recognition accuracy.
- Never upload a local runtime folder, real captions, screenshots, login profile, media, or response JSON to the public GitHub repository or CI artifacts.

## Offline checks

```powershell
python -m unittest discover -s tests -p test_agy_media_probe.py -v
```

The offline tests use only synthetic data. CI does not open an AGY session, contact YouTube, or perform a real multimodal model call.

### Independently inspect AGY read_file calls without publishing the transcript

After a local `-Transport file-read -Submit` experiment, search AGY's
conversation transcript under `~.geminiantigravity-clirain` for
the relevant result filename, then pass the matching `transcript_full.jsonl`
to the offline inspector:

```powershell
$brain = Join-Path $HOME ".gemini\antigravity-cli\brain"
$found = @(Get-ChildItem -LiteralPath $brain -Recurse -File -Filter transcript_full.jsonl -ErrorAction SilentlyContinue | Where-Object { Select-String -LiteralPath $_.FullName -SimpleMatch "flash_audio_file_read_mp3.json" -Quiet } | Sort-Object LastWriteTime -Descending)
if ($found.Count -eq 0) { throw "Matching AGY transcript not found" }
$trace = $found[0].FullName
python .\tools\inspect_agy_read_file_trace.py --trace $trace --target clip_00_30.mp3 --schema-preview
```

This command **does not call a model or modify the original transcript**. It prints
only the tool-call step number and tool name, whether the *arguments* contain
the target basename, whether a tool-result record appears at that step, and
whether a structural MIME/payload *hint* was present. It never prints raw
arguments, private paths, audio bytes, model transcripts or full tool returns.

Interpretation is deliberately conservative: `DONE` only proves
the tool invocation completed. `target_file_calls > 0` proves the target
filename appeared in one call's arguments. `audio_mime_hint` and
`inline_payload_hint` **still do not establish audio reached or was
understood by the model**, and lack of hints in serialized JSONL is not proof
that no internal media transfer occurred. Ground-truth accuracy requires
independent review of the original audio.


The optional `--schema-preview` inspects field names and value types
for the target call and up to four subsequent JSONL records. This helps determine
whether tool results are serialized as a separate event instead of being embedded
in the same `tool_calls` record. Unknown dictionary keys are replaced
with a count; all actual field values, text, paths, media bytes and payloads
remain excluded. A shape hint never constitutes proof of model audio reception.


### Tool call -> adjacent result step correlation

The `--schema-preview` mode also emits `adjacent_result_candidates`.
AGY's JSONL trajectories typically record a `PLANNER_RESPONSE` tool call,
followed by a separate `GENERIC` (or `VIEW_FILE`) result step.
For the local MP3 experiment, Step 1 invoked `read_file` on the target
and Step 2 is a `content`-bearing record. The new output reports:
the next step's allowlisted `event_type`, whether it resembles a tool
result, `content_character_count`, and whether a textual audio MIME
marker appears. It **never publishes the content or raw event label values**.

A matching adjacent `GENERIC` record is **evidence of a tool-result
record**, not proof the result contained playable MP3, that binary bytes were
provided as model audio input, or that resulting transcription matches
the original audio. Preserve `independent_audio_receipt_verified=false`
until controlled, audio-grounded testing supports a stronger assertion.
