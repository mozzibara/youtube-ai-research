# Public distribution: research-material and execution boundaries

## Absolute Git-publication prohibition

This **public source distribution does not support publishing research materials via Git**. The Python `publish_batch()` function always raises `PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED`, before reading the source registry, running Git, or touching the supplied root. The `publish-batch` CLI subcommand is rejected during argument validation, even when `--root` points outside this checkout.

The arguments `--commit`, `--push`, `--acknowledge-rights` and `--allow-whitespace-warnings` do not override this restriction. A `.gitignore` file is *not* a publication security boundary because previously staged or force-added files remain trackable. Never remove the explicit block for convenience.

The original private reference implementation is retained as historical research material. Publication of any private corpus requires a **separately approved and independently reviewed private application workflow**; it is not a toggle inside the public implementation.

## Separate responsibilities

| Public Git repository | Private runtime / archive |
| --- | --- |
| Python source and synthetic regression fixtures | Source registry, source URLs and media |
| Offline CI, reviewed pull requests | Original transcripts and immutable model responses |
| Safe aggregate audit metadata | Actual evidence and reviewer records |
| Documented contracts and failure states | Approved and access-controlled output destinations |

The presence of a README, source code, or passing test must never be interpreted as rights to republish a transcript or video. Private data must not be uploaded to a public Actions artifact, PR comment, issue, log, cache key or report.

## Other capabilities and limitations

This package still contains commands that can access media or model providers if explicitly invoked with real inputs. **The default GitHub CI performs synthetic offline validation only**; it is not an OS-wide network sandbox. Actual AGY/YouTube operations remain subject to explicit owner authorization, provider permissions, licensing and private runtime credentials.

No production worker is connected by Phase 5E.1. Queue jobs marked `SIMULATED` do not mean source analysis completed. Neither a structural PASS nor a cross-model check is human report approval.

## Regression acceptance

- Direct API invocation, alternate roots, all publication flags: no Git subprocess, no corpus read, no write
- CLI `publish-batch`: parse-time refusal, exit code 2, no filesystem side effect
- Git file allowlist and token-like literal audit: PASS
- Windows and Ubuntu full offline unit tests + 16/16 Phase 5E scenarios + Phase 5D 12x3 stress: PASS before merge
