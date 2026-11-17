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

### Definitive wrong tone-count evidence in strict-rejected JSON

The second full v3 session also returned an unrecoverable tone-only answer
with `verdict=NO_SPEECH`, `acoustic_class=TONE`,
`audio_analyzed=true`, zero speech segments, and an integer
`beep_count` that contradicted the v3 tone contract.
Since valid tone input in this harness always contains a **private
2–5-beep count**, and the strict parser permits a TONE integer only within
2–5, these specific conditions prove a wrong count without revealing the
reported or expected count.

The evaluator now emits
`confirmed_tone_count_error=true`, with the affected trial
marked `FAIL_OBSERVED_TONE_COUNT_ERROR` and the evidence source
`REJECTED_TERMINAL_RESPONSE`. It does **not** recover the
invalid JSON, change `all_complete=false`, guess the observed
beep number, or overwrite any archived files. For a valid recovered TONE
answer whose count differs from the hidden reference, the evidence source
is instead `VALIDATED_PRIVATE_TERMINAL_RESPONSE`.

Non-integer, null, missing or boolean beep-count fields remain contract
failures, but are **not** promoted to demonstrated acoustic counting errors
because the model did not provide a valid integer count. Provenance, status,
and unchanged media hashes must also be present.

Re-evaluate offline with no further model requests:

```powershell
git pull --ff-only origin main
python .\tools\agy_blind_audio.py evaluate --session blind-ecc67afd26534f13 --max-cer 0.25
```

The validated findings now distinguish:
(1) speech transcription accuracy, (2) silent-audio false tone detection,
and (3) tone count mistakes, rather than labeling (3) as missing output.

### Full 3+1+1 session: confirmed silence false positive despite an incomplete tone

The independent full session `blind-ecc67afd26534f13`
ran **3 Korean speech + 1 random tone + 1 exact silence** trials.
All three speech trials correctly restored their private random nonces
(CER 0.0). The silent MP3 was classified as `TONE` even though
its JSON was a **strict-valid** `NO_SPEECH/TONE` answer.
The tone-only trial separately returned an unparseable final v3 JSON with
the claimed `TONE` class and an inconsistent beep_count.
The invalid TONE integer is outside the allowed 2–5-beep range;
the enhanced evaluator now reports it as a confirmed count error, while
keeping strict recovery incomplete.

The evaluator now recognizes confirmed wrong acoustic classes from both:
(1) valid host-captured answers and (2) otherwise structured but rejected
terminal answers. Both cases require matching private media hashes, successful
CLI execution, the target MP3 read_file event and no disallowed tools.
If a valid silence answer says `TONE`, the report includes
`acoustic_misclassification_evidence_source=VALIDATED_PRIVATE_TERMINAL_RESPONSE`
and `overall=FAIL_AUDIO_DISCRIMINATION`, even when another
trial is still missing and `all_complete=false`. This prevents
a confirmed acoustic false positive from being hidden by an unrelated
unparseable output. It does not salvage the malformed tone answer or
overwrite evidence.

Recalculate the existing session with **zero AGY requests**:

```powershell
git pull --ff-only origin main
python .\tools\agy_blind_audio.py evaluate --session blind-ecc67afd26534f13 --max-cer 0.25
python .\tools\agy_blind_audio.py diagnose-response --session blind-ecc67afd26534f13
```

Treat a model's `NO_SPEECH` response separately from its
`acoustic_class`: speech absence can be correct even as
beep/tone presence is hallucinated. Three successful synthetic speech
trials do not establish real YouTube-news transcription performance.

### Verified silent-audio misclassification (archived v3 session)

The existing session `blind-cbbaf1380fa34098` has a strong but
mixed behavioral result:

- Korean speech: correct transcription, private six-digit nonce, CER 0.0
- Random beep-only audio: correct TONE class and private count
- Silence `case-02f20b1521f5`: the terminal response claims
  `NO_SPEECH`, `audio_analyzed=true`, and
  `acoustic_class=TONE` although the private ground-truth
  class is **SILENCE**. Its integer beep_count also conflicts with the
  strict TONE contract, so the response must **not** be auto-recovered as
  a valid structured answer.

This is an observed **acoustic false positive**, not simply missing output.
The scorer now checks whether a rejected terminal JSON contains a clear
incorrect acoustic class with a valid NO_SPEECH claim, 0 segments and
analyzed flag, **only** when audio hashes, successful CLI status and
read_file-only tool provenance all agree. In that narrow case it reports:

`overall=FAIL_AUDIO_DISCRIMINATION` and
`observed_acoustic_misclassification=true` for the affected
trial. `all_complete=false` correctly indicates that the
strict v3 response was not recoverable; it is not a behavioral pass.

Any malformed text, unknown class, missing terminal response or unverified
provenance still stays `INCOMPLETE`. Original private AGY streams,
media and model responses are never rewritten, and no extra model requests
are made when re-evaluating:

```powershell
git pull --ff-only origin main
python .\tools\agy_blind_audio.py evaluate --session blind-cbbaf1380fa34098 --max-cer 0.25
```

This behavioral evidence supports real speech/tone perception, but not
independent proof of internal audio binary transfer. A one-trial failure
does not establish a general silence error rate.

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
key. The diagnostic itself never alters existing archived evidence. The
separate evaluator may report an explicit behavioral FAIL when an invalid
response contains a confirmed, ground-truth acoustic class mismatch;
otherwise ambiguous responses remain INCOMPLETE.

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

## Offline MP3 byte visibility audit (separate from acoustic scoring)

Run this new command on **already archived** session data. It is read-only
and makes **zero AGY/model calls**. Do not repeat `evaluate`, `recover`,
or completed trials merely to conduct this audit.

```powershell
$session = "blind-ecc67afd26534f13"
python .\tools\agy_audio_transport_audit.py --session $session
```

The default private root is
`%LOCALAPPDATA%\AGYBlindAudio\truth`. To use a different private root,
supply `--private-root` pointing to the **truth root**, not its session
subdirectory. The reporter reads the original manifest and per-trial
`*.ndjson` streams without storing, changing, or emitting audio content,
ground truth, model transcripts, original paths, or credential values.

The meter accepts two carefully scoped sources of CLI tool-result bytes:
(1) an audio MIME + base64 payload nested under `tool_info.result/output/response`
for the **target** `read_file` event; or (2) a separately typed `TOOL`
result whose `tool_call_id` matches that target event. Adjacent generic
messages, model-authored JSON, tool-call arguments, filenames, MIME hints,
and arbitrary transcript text **never** count as byte transport evidence.
A matched payload must decode to the **exact** locally archived MP3 bytes,
with the original private manifest's SHA-256 also matching the source file.

Interpret `assessment` as follows:

- `MATCHED_CLI_TOOL_RESULT_BYTES`: exact source MP3 bytes appeared in
  a correlated, structured AGY CLI tool result. This is a **CLI-stream
  byte match**, *not* independently proven model ingestion.
- `TARGET_READ_CALL_SHAPE_UNSUPPORTED`: the established blind evaluator
  observed the target MP3 read call, but the stricter byte-audit parser could
  not correlate its parameter envelope. This is a **protocol/parser
  compatibility gap**, not evidence that no read occurred. The audit reports
  `legacy_provenance_only=true` and never promotes an uncorrelated audio
  payload to a byte match.
- `NO_TARGET_READ_CALL`: neither the strict parser nor the existing
  evaluator observed a target read call in the available typed stream;
  it does not prove one never occurred in AGY internals.
- `NO_TOOL_RESULT_AUDIO_BYTES`: an MP3 read call occurred, but the stream
  exposed no verifiable inline audio bytes. This is **inconclusive**, not
  evidence that the model did not receive audio.
- `CLI_TOOL_RESULT_BYTES_MISMATCH`: at least one decoded audio payload
  differed from the MP3. Transcoding, a different tool artifact, or faulty
  source correlation must be investigated without rewriting the archive.
- `INVALID_TOOL_RESULT_ENCODING`, `INCOMPLETE_TRACE`,
  `UNSAFE_TRACE`, `MEDIA_HASH_MISMATCH`, or `TRACE_MISSING`:
  the available artifacts cannot support an affirmative byte match.

Both `model_ingress_verified` and
`independent_binary_transport_verified` deliberately remain
`false` for **every** possible CLI-only audit outcome. This avoids
confusing local tool-result visibility with an authenticated trace at
the provider's multimodal request boundary. Confirming provider/model
ingress requires a separately trusted observation of the actual request
media payload (or an authenticated provider-side receipt); neither is
available merely from `stream-json`. The tool neither intercepts TLS
credentials nor changes the user's AGY permissions.

CI uses fabricated MP3-like bytes and synthetic tool event fixtures only.
Real private audio is never published to Git or GitHub Actions.


### Redacted event-envelope probe after unsupported target-read shapes

The archived full 3+1+1 session `blind-ecc67afd26534f13` returned
`TARGET_READ_CALL_SHAPE_UNSUPPORTED` for five cases under the newer
byte-level parser. The earlier blind evaluator had already observed five
target `read_file` calls. **This is a parser-compatibility gap, not five
new failures to read audio**; it neither confirms nor denies MP3 delivery.

Run the new *different* offline shape probe once; there is no reason to
re-run `evaluate`, `diagnose-response`, `recover`, or the AGY model:

```powershell
git pull --ff-only origin feature/agy-audio-byte-receipt-audit
python .\tools\agy_audio_transport_audit.py --session blind-ecc67afd26534f13 --inspect-shape
```

The probe reads only existing `*.ndjson` metadata and emits a fixed,
privacy-safe vocabulary: parameter envelope type, allowlisted known
parameter **key names**, count of unknown top-level keys, known tool-name
location, and whether known tool result envelope keys exist. It **never**
prints unknown dictionary keys, parameter values, paths, audio data,
transcripts, credentials, prompt content or model answers. It never
downloads data, invokes AGY, or modifies archived evidence.

A `UNSCOPED_LEGACY_REFERENCE` is specifically not a trusted audio byte
receipt: the old inspector found the target string somewhere in serialized
parameters, while the newer strict parser needs an explicit typed input
path. Use the shape-probe output to design a constrained parser for the
actually observed AGY schema, with synthetic red/green fixtures. Do not
accept an arbitrary nested string match as proof of audio transfer or
silently turn the known five trials into a `PASS`.


### Offline lifecycle inspection after 2 read_file events per case

The archived `blind-ecc67afd26534f13` session had **two**
`read_file` step-update events in each of five cases, an object-shaped
parameter envelope with two unrecognized keys, and no inline
`tool_info.result/output/response` fields. These findings are already
recorded and must not be re-collected merely to repeat the same checks.

To determine if the two *events* share a step index or call identifier,
and what typed events directly follow the read calls, run this **new**
offline inspection once:

```powershell
git pull --ff-only origin feature/agy-audio-byte-receipt-audit
python .\tools\agy_audio_transport_audit.py --session blind-ecc67afd26534f13 --inspect-lifecycle
```

This produces only allowlisted categories and numeric counts:
`target_tool_event_count`, `distinct_step_index_count`,
`repeated_step_index_groups`, `distinct_call_id_count`,
`missing_call_id_event_count`, `distinct_argument_envelope_count`,
`repeat_update_possible`, `tool_step_statuses`,
`same_step_status_change_observed`, `target_reference_location`,
`post_target_event_types`, and `correlation_level`.
Raw step IDs, tool call IDs, argument key names/values, reference paths,
transcripts, decoded media bytes, or model output are never printed.

- Two step events with the **same step index or tool call ID** may be
  repeated progress updates of **one** actual read; the report never
  claims they prove two separate reads.
- A target reference in an unknown field is classified as
  `DIRECT_VALUE`, `NESTED_VALUE`, or `KEY_ONLY`, without exposing
  its key name or promoting it to an approved file-path binding.
- A following `GENERIC` or terminal event yields
  `ADJACENCY_ONLY`: mere adjacency is **not a tool receipt**.
  `EXPLICIT_TOOL_CALL_ID` requires an allowlisted TOOL-source result
  and a matching ID, but still does **not** prove model ingress.
- `verified_distinct_file_reads`, `model_ingress_verified`, and
  `independent_binary_transport_verified` remain `false` in all
  CLI-only lifecycle reports.

The command does not modify private files or invoke AGY. Its purpose
is to identify whether a narrow, evidence-backed follow-up parser is
possible. Avoid adding speculative aliases or treating untrusted
tool-call argument text as proof of MP3 byte delivery.


### Single next check: typed return envelope after deduplicated read_file events

For the archived session `blind-ecc67afd26534f13`, the five trials each
already have exactly **two AGY stream events referencing the MP3 under
one step index**. No tool call ID or explicit tool-result correlation
was found, and the recorded step status was an unknown/missing value.
This establishes *one distinct observed tool step per trial*, not two
independent audio reads and not confirmed transport to Gemini.

The new `--inspect-return-envelope` mode examines **only the first four
event records after the last target read_file step update**. It reveals
fixed-label envelopes and scalar *types*, never actual contents.
It is intentionally narrower than another session evaluation:

```powershell
git pull --ff-only origin feature/agy-audio-byte-receipt-audit
python .\tools\agy_audio_transport_audit.py --session blind-ecc67afd26534f13 --inspect-return-envelope
```

It prints `target_status_slots` (presence/type at the existing known
`step.status` and `tool_info.status` paths),
`target_known_status_labels` (only enumerated values),
and `adjacent_envelopes` with an allowlisted event kind, step subtype,
content type, source label and selected standard **field names**. Unknown
keys/values, paths, model text, media, and any tokens remain private.

Interpret `return_assessment` conservatively:

- `ADJACENT_UNTYPED_EVENTS_ONLY`: surrounding events exist, but no typed
  `TOOL`-source result is evidenced in the selected window.
- `UNLINKED_TYPED_RESULT_CANDIDATE`: a typed result candidate was
  observed, **not** proven to belong to the target read call, and not
  proven to contain MP3 audio or reach Gemini.
- `NO_POST_TARGET_EVENT`, `NO_TARGET_EVENT`,
  `INCOMPLETE_TRACE`, `TRACE_MISSING`, or `UNSAFE_TRACE`
  are inconclusive for the audio delivery question.

A model-generated terminal `result.response` is **not** a tool return,
even if its text mentions an MP3, MIME label, or transcription. A
structured result without a matching call ID likewise cannot be
promoted to verified byte transport. All independent ingress flags
remain false. No new AGY invocation or prior trial replay is needed.
