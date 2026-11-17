# AGY Flash blind Korean speech vs silence A/B experiment

The repository contains **source and synthetic tests only**. Never commit a real
MP3, private answer key, model response, raw AGY event stream or transcript.

## Purpose and evidence tiers

The previous SBS trial self-reported speech recognition and the trace confirmed
`read_file(clip_00_30.mp3)` followed by a typed result candidate.
That does not independently establish that actual audio bytes reached Gemini.
A blinded trial helps distinguish real audio comprehension from guessing or
text leakage, without exposing the text of the voice recording in a model prompt.

The harness creates **three randomized Korean speech samples** and **two exact
30-second silent MP3 samples** by default. Case directory names and prompts do
not reveal the class or the spoken text. Each spoken sample contains a random
six-digit Korean sound sequence, never present in the model's workspace except
as **sound**.

### Prerequisites

Windows PowerShell 5.1; an enabled `ko-KR` Windows System.Speech
voice; `ffmpeg` and `ffprobe` on PATH;
Python 3.12+ and authenticated AGY CLI. AGY uses the installed user's model
entitlement. This harness does not install voices or dependencies.

Check available Korean voice locally:

```powershell
Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.GetInstalledVoices() | Select-Object -ExpandProperty VoiceInfo | Select-Object Name, Culture; $s.Dispose()
```

If no `ko-KR` voice exists, generation **fails closed**.
Do not substitute a non-Korean voice or fabricate a ground truth. Install
an appropriate Korean SAPI voice through your normal Windows language/voice
setup before retrying. Windows OneCore voices and System.Speech voices are not
necessarily interchangeable.

### Step 1 — Generate media without model calls

In the existing project Git checkout:

```powershell
git pull --ff-only origin main
python .\tools\agy_blind_audio.py prepare
```

The command prints a session ID, e.g. `blind-<random-hex>`.
Copy it into a PowerShell variable (replace the placeholder with the real value):

```powershell
$session = "blind-REPLACE_WITH_16_HEX_DIGITS"
python .\tools\agy_blind_audio.py run --session $session --trial all
```

The second command is a **no-model-call** preview, listing case identifiers.

Default roots (Windows): `%LOCALAPPDATA%AGYBlindAudiountime` and
`%LOCALAPPDATA%AGYBlindAudio	ruth`. They are separate and
outside the public Git checkout. The private manifest includes the actual
spoken text, nonce and case-to-condition mapping; **never expose it to AGY,
upload it, or use its phrase in any model prompt**. Media generation temporarily
uses WAV within the private folder, deleting that WAV when MP3 creation ends.
Only MP3 is copied into each trial workspace.

### Step 2 — Real AGY trials (explicit opt-in; uses model quota)

Inspect a dry preview first, then request the full sequential five-trial run:

```powershell
python .\tools\agy_blind_audio.py run --session $session --trial all --submit
```

Each trial starts a new AGY CLI process, with:

- `-p`, `--model gemini-3.8-flash-medium`,
  `--output-format stream-json`, `--sandbox`
- Only `clip_00_30.mp3` in its workspace; the model is asked to
  use `read_file` on that file alone and to write `answer.json`
- Explicit separate `NO_SPEECH` and `NO_AUDIO_ACCESS` verdicts
- Full raw AGY stream and stderr stored **only in the private folder**

By default permission prompts are **not** bypassed. If the headless CLI
cannot obtain normal workspace read/write permissions and you are operating
inside an isolated environment, you can explicitly opt into
`--auto-approve` alongside `--submit`.
This uses AGY's `--dangerously-skip-permissions` flag.
**Do not use that opt-in on a personal workstation without confirming that
AGY's sandbox is effective**: on some Windows versions `--sandbox`
is a no-op. The harness cannot prove permission confinement.

Never run the same trial twice: existing outputs and logs are not overwritten.

### Step 3 — Evaluate (offline; does not call any model)

```powershell
python .\tools\agy_blind_audio.py evaluate --session $session --max-cer 0.25
```

The private report includes only per-case flags, CER scores, nonce matches
and silent-case hallucination indicators. It **does not print answer keys or
full transcripts**.

A behavioral PASS requires:

1. At least 3 speech and 2 silence trials fully completed.
2. A successful AGY terminal event **and** actual `read_file` tool
   steps targeting `clip_00_30.mp3` in each trial's structured event log,
   with no other model tool calls except allowed `write_file`.
3. For **every** speech trial, nonempty model transcription, six-digit Korean
   nonce correctly heard and character error rate (CER) at most 0.25.
4. For **every** silence trial, explicit `NO_SPEECH` with no segments
   and no invented text.

A failed/missing AGY call is INCOMPLETE, not success. Smaller experiments
are PROVISIONAL. A behavioral pass establishes strong evidence of audio
discrimination **but does not independently verify internal binary transport**
or imply zero error in real-world news transcription.

### Security & limitations

- The randomized answer key is plaintext only in the *separate private root*.
  Do not put that root under the AGY workspace or Git checkout.
- Process isolation is not a cryptographic guarantee: AGY permissions may
  permit access outside the workspace. Check the typed tool stream for other
  file/command calls, and prefer isolated credentials/workstation.
- Synthetic SAPI speech is easier than spontaneous speech; passing the test
  does not validate real-world speaker diversity or timestamp alignment.
- Unit tests mock the audio encoder and agent and never spend a model token.
  Actual speech synthesis and headless AGY response must be checked on Windows.
- AGY headless `stream-json` includes tool activity; unknown protocol
  variants fail closed rather than invent success.
