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
