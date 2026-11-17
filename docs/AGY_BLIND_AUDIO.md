# AGY Flash blind three-condition audio experiment

The current v3 benchmark uses **Korean speech, audible beep-only audio,
and exact silence** as three independently blinded classes.

The repository contains **source and synthetic tests only**. Never commit a real
MP3, private answer key, model response, raw AGY event stream or transcript.

## Purpose and evidence tiers

The previous SBS trial self-reported speech recognition and the trace confirmed
`read_file(clip_00_30.mp3)` followed by a typed result candidate.
That does not independently establish that actual audio bytes reached Gemini.
A blinded trial helps distinguish real audio comprehension from guessing or
text leakage, without exposing the text of the voice recording in a model prompt.

The harness creates **three randomized Korean speech samples, one audible
beep-only MP3 and one exact silent MP3** by default. All files are 30-second
mono MP3s with opaque case names. Spoken samples include randomized six-digit
Korean sound sequences. A beep-only MP3 contains **2–5 distinct 920 Hz tones**;
the randomly selected beep count stays in the separate private answer key.
Neither spoken phrases nor the beep count appears in the agent's prompt or
workspace except as actual audio.

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

Default roots (Windows): `%LOCALAPPDATA%\AGYBlindAudiountime` and
`%LOCALAPPDATA%\AGYBlindAudio	ruth`. They are separate and
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
  use `read_file` on that file alone and return exactly one JSON object
  as its final answer. It is explicitly forbidden to write any files.
- The local Python host saves a validated final JSON response **only to
  the private truth folder**, never into the AGY workspace.
- Explicit `AUDIO_TRANSCRIBED`, `NO_SPEECH` and `NO_AUDIO_ACCESS`
  verdicts **plus** `acoustic_class` and `beep_count` for new v3 trials
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

### Headless write_file auto-denial — v2 host-owned JSON output

In a real five-trial Headless run, each MP3 read was attempted but the CLI
later **auto-denied `write_file`** when the agent tried to save
`answer.json`. All five runs returned an empty final response.

The updated v2 prompt **never asks the agent to create any file**. Only
`read_file` of the target MP3 is permitted. The model is asked
to return exactly one final JSON object, and the Python host captures
that JSON from the private `stream-json` terminal result.
Any model-side file write invalidates a trial regardless of its transcript.

**Retain the original session unchanged.** It cannot be rerun with a new
prompt under the same trial ID because the benchmark deliberately
prevents overwriting evidence. Start with one new speech, one beep-only and one silence
case to minimize cost:

```powershell
git pull --ff-only origin main
$session = (python .\tools\agy_blind_audio.py prepare --speech-count 1 --tone-count 1 --silence-count 1 | ConvertFrom-Json).session
python .\tools\agy_blind_audio.py run --session $session --trial all
```

The dry run above performs **zero model requests**. To explicitly initiate
the three model requests (only after checking the workspaces and permissions):

```powershell
python .\tools\agy_blind_audio.py run --session $session --trial all --submit
python .\tools\agy_blind_audio.py evaluate --session $session --max-cer 0.25
```

A 1+1+1 trial cannot yield the full 3+1+1 benchmark PASS; if all cases meet
their behavioral checks its correct verdict is
`PROVISIONAL_INSUFFICIENT_TRIALS`. If Headless still
returns empty output, diagnose that **new** session offline with
`agy_blind_diagnose.py`; do not bypass permissions or repeat
the same trial.

### Offline diagnosis of an unparseable final response

A real 1+1+1 v3 session `blind-cbbaf1380fa34098` yielded a
passing spoken-Korean trial (CER 0.0, nonce match) and a passing beep-only
trial (correct acoustic class and secret beep count). The silence trial
`case-02f20b1521f5` exited with SUCCESS and read_file provenance
but its final result did **not** satisfy the strict v3 JSON contract. It
must remain INCOMPLETE until the failure is classified: this is not proof
of fabricated speech or of an inability to perceive silence.

To diagnose the existing archived responses without further model calls:

```powershell
git pull --ff-only origin main
python .\tools\agy_blind_audio.py diagnose-response --session blind-cbbaf1380fa34098
```

This command is **read-only**. It never writes or prints full model output,
transcriptions, unknown field names/values, MP3, private references,
nonce answers or stderr. It outputs only allowlisted labels for:

- `INVALID_JSON_SYNTAX_OR_ADDITIONAL_TEXT`: final output is not
  a standalone JSON object
- `AUDIO_ANALYZED_NOT_BOOLEAN`: wrong type or missing field
- `VERDICT_SEGMENTS_OR_ANALYSIS_CONFLICT`: verdict and
  speech segments or analyzed flag disagree
- `ACOUSTIC_CLASS_MISSING_OR_INVALID`: acoustic class absent
  or outside the supported fixed enum
- `ACOUSTIC_VERDICT_OR_BEEP_COUNT_CONFLICT`: class and beep
  count are inconsistent with the verdict
- `VALID_TERMINAL_JSON`: strict parser accepts the output

Other categories are similarly constrained. Known reported verdict/class
labels reflect **only what the model claimed**, never the hidden answer
key. The diagnostics do not infer actual audio binary transport or change
the prior `INCOMPLETE` report.

If the terminal response is merely malformed, review the contract
mismatch before deciding whether a narrowly scoped, source-preserving
offline parser improvement is justified. Do **not** re-run completed trials
or silently convert `NO_AUDIO_ACCESS` to `NO_SPEECH`.

### Three-condition acoustic classification and blinded beep count

New CLI defaults: `--speech-count 3 --tone-count 1 --silence-count 1`.
To limit model quota for an initial check, use 1+1+1 as documented above.
The v3 response contract has private manifest schema `agy_blind_audio_v2`,
and model output contract `host_terminal_json_v3`. Old sessions
continue to use their original parser and thresholds.

The model must output exactly one JSON object. For example, a beep-only
response can have this **illustrative structure** (the number shown is
not part of any private trial):

```json
{
  "verdict": "NO_SPEECH",
  "audio_analyzed": true,
  "acoustic_class": "TONE",
  "beep_count": 3,
  "segments": []
}
```

The secret 2–5 beep count is generated cryptographically and the PCM WAV
is synthesized locally in Python before FFmpeg encodes the same 30-second
MP3 format. The intermediate WAV is deleted. The scorer rejects a wrong
tone count, a beep file mislabeled SILENCE, any invented speech, or a
NO_AUDIO_ACCESS answer. This isolates acoustic access from speech content.

The agent may use only read_file on the target MP3; the host captures the
structured final response to its private folder without a model-side write.
Neither an apparent tool call nor a passing response independently verifies
the internal binary media transport, so that separate caveat remains.

### Diagnose an empty Headless SUCCESS without spending more quota

A real five-trial legacy v1 session produced five `read_file` attempts,
five CLI exit-zero / terminal SUCCESS events, no `answer.json` and
no nonempty terminal response. Local stderr fingerprints identified an
automatic **`write_file` denial** in all five cases. This means
**INCOMPLETE**, not proven transcription failure or success.

AGY Headless may soft-deny unapproved tool operations and still exit zero.
A terminal SUCCESS with an empty response has also been reported in AGY.
Do not assume an observed tool *request* was actually allowed or completed.

Inspect only metadata from the existing archived private NDJSON and stderr:

```powershell
$session = "blind-84c9e816a9a54495"
python .\tools\agy_blind_diagnose.py --session $session
```

The diagnostic does not invoke AGY or reproduce any text from transcripts,
tool output, stderr or hidden answer keys. Per case it reports terminal
status, final-response length, presence and length of model `agent_response`
deltas, target MP3 read attempts, tool result state markers and **hints**
of explicit permission denial. `POSSIBLE_TOOL_PERMISSION_DENIAL_OR_ERROR`
means denial language was observed, not a reconstructed proof of why it
failed. `EMPTY_TERMINAL_RESPONSE_AGENT_TEXT_PRESENT` means that
model text fragments may have been streamed but terminal response was
empty; analyze that category separately before considering any replay.
`SUCCESS_OR_OTHER_TERMINAL_WITH_NO_MODEL_TEXT` means no usable
answer was found, not that the MP3 contained no speech.

#### Distinguish *which* permission was denied

The local `agy_blind_diagnose.py` output includes
`stderr_permission_fingerprint`, which reports **only allowlisted
labels**, never original stderr text or paths:

- `signature=HEADLESS_AUTO_DENIAL`: Headless refusal wording detected.
  Check `headless_required_permission_kind` to distinguish an explicit
  `read_file`, `write_file` or `command` permission.
- `signature=EXPLICIT_USER_DENY_RULE`: An explicit user-configured
  deny rule appears to be involved. Do not bypass it.
- `signature=SANDBOX_OR_APPCONTAINER_RESTRICTION`: OS isolation or
  sandbox text is associated with the denial.
- `signature=FILESYSTEM_ACCESS_DENIED`: A local access error is
  suggested, but may not be an AGY policy issue.
- `signature=GENERIC_PERMISSION_DENIAL`: Generic denial with insufficient
  detail; further review is necessary.

A tool name in `known_tools_mentioned` is **only a mention** in stderr
and may occur in explanatory examples. The separate
`headless_required_permission_kind` is populated only when a
known permission name occurs in the more specific documented
`tool required the "..." permission` form.

The Antigravity CLI release notes document headless soft-denials for tools
which need confirmation. The CLI's terminal `SUCCESS` and process
exit code 0 therefore cannot be used as a permission grant or proof of model
audio perception.

Do **not** rerun completed cases, modify existing AGY permission settings
or enable `--dangerously-skip-permissions` based solely on empty
terminal responses. A narrowly scoped policy change should only be
considered after the private diagnostic explicitly identifies a denied
operation and the actual CLI workspace/sandbox boundary has been checked.

### Recover existing final model responses when answer.json was not written

On Windows AGY Headless, a model request may complete successfully and
record a `read_file` invocation without creating `answer.json`.
**Do not rerun completed trials.** The CLI's `--output-format stream-json`
terminal `result.response` may contain the model's final JSON.

For an existing session, use **offline recovery with no model calls**:

```powershell
$session = "blind-REPLACE_WITH_EXISTING_16_HEX_DIGITS"
python .\tools\agy_blind_audio.py recover --session $session
python .\tools\agy_blind_audio.py evaluate --session $session --max-cer 0.25
```

Recovery requires a matching unchanged MP3 hash, existing successful CLI
status, an observed MP3 `read_file` tool call in the archived stream,
no disallowed tools, exactly one successful terminal result and strictly
parseable JSON with a valid verdict/segment schema.

The extracted response goes only into the separate private root:
`%LOCALAPPDATA%\AGYBlindAudio\truth\<session>\recoveries`.
It is **never copied into the model workspace**, published, or printed. The
original AGY stdout/stderr and any preexisting answer are never overwritten.
`UNPARSEABLE_TERMINAL_JSON` or `EMPTY_TERMINAL_RESPONSE`
means the response cannot be recovered from this format. It is **not** evidence
that the audio was unheard.

For future runs, the harness automatically attempts this strict recovery
once after a successful headless request. It still records the provenance
separately; a model reply is not proof of the backend binary transport.

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
   with **no other model tool calls**, including `write_file`. For
   the v2 output contract, the only accepted answer source is
   `private_terminal_recovery` from the host.
3. Every speech trial must restore the random six-digit nonce, report
   acoustic_class=SPEECH and beep_count=0, with CER at most 0.25.
4. Every beep-only trial must report verdict=NO_SPEECH,
   acoustic_class=TONE and the **exact private beep count** with no segments.
5. Every silence trial must report verdict=NO_SPEECH,
   acoustic_class=SILENCE and beep_count=0 with no segments.
6. All successful trials require audio_analyzed=true. A NO_AUDIO_ACCESS
   verdict always fails the behavioral test, including for complete silence.

The current v3 full benchmark requires at least **3 speech + 1 beep + 1
silence** trials. Archived v1 manifests use their original **3 speech + 2
silence** requirement.

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
- Strict response recovery accepts only a single JSON object (or an entire
  fenced JSON block); descriptive prose with embedded JSON is not treated
  as a verified structured response. Explicit agent replies can also be
  incomplete despite a successful CLI exit.
- Unit tests mock the audio encoder and agent and never spend a model token.
  Actual speech synthesis and headless AGY response must be checked on Windows.
- AGY headless `stream-json` includes tool activity; unknown protocol
  variants fail closed rather than invent success.
