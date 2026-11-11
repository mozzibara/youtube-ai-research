# Legacy Private research and implementation handoff

**Reference baseline:** historical private Phase 5E.1 source. **Development target:** public `mozzibara/youtube-ai-research`, currently Draft PR #1. This document transmits only implementation facts, defects and test priorities. Do not import private media, full source registry, original Git history or raw model output.

## 1. Audited comparison and selection

The private migration baseline contains **127 tracked files**; the initial Public PR snapshot has **76**. Comparison by Git Blob SHA found **57 byte-identical files**, **16 intentionally changed files**, **54 private-only files** and **3 public-only safety/guide files**.

All 31 production Python files and 28 legacy regression test files were present in the initial Public snapshot. The changes included sanitized synthetic IDs and the intentionally different CI/README. The private-only items comprised 42 research/policy/source artifacts, 11 technical documents and 1 raw video benchmark.

**Do not blindly copy old source files.** Public safety restrictions and synthetic fixtures intentionally differ. The original private `main` has a much older codebase than the private Phase 5E.1 branch, so comparing only `main` would obscure the implemented code.

## 2. Recovered technical decisions

| Priority | Historical finding | Public implementation policy |
| --- | --- | --- |
| P0 | The old source publisher could stage and Git-push transcripts and reports | Public publisher is unconditionally disabled at API and CLI level |
| P0 | Lease, owner, attempt and budget reservation may change after pre-validation | Revalidate them inside the SQLite write transaction |
| P0 | 429 or unknown provider usage can leave unsettled cost | Global manual pause and held reservation; never assume zero use |
| P0 | Source URLs, media access and tool availability are different kinds of evidence | A successful tool event and semantic review are separate requirements |
| P1 | Structural evidence references can be correct while the cited content is wrong | Keep structural checks separate from independent semantic verification |
| P1 | An unverified claim is not automatically a high-risk or false claim | Preserve independent verification status, risk signals and review priority |
| P1 | The original 19-section report had unresolved audit findings | Historical report remains NOT_APPROVABLE; no retrospective approval |
| P1 | Synthetic checkpoint completion was misinterpretable as real completion | Keep Phase 5D simulation separate from verified artifacts and live work |
| P1 | Actual Research Board panel indices are global within their family | Test renderer metadata and exact referenced panel numbers |
| P1 | Missing CLI prerequisites could overwrite earlier pipeline status | Precheck input and preserve previous stage on missing artifacts |

## 3. Historical branch audit

Most old feature/fix branches were ancestors of the private Phase 5E.1 baseline; do not reapply them. Two diverged branches contained unique changes:

- `fix/cli-prerequisites-and-status`: reusable missing-prerequisite behavior; adapt tests to synthetic video IDs and current CLI, not to old implementation wholesale.
- `fix/evidence-reference-validation-20261110`: old evidence-board PNG, parser and CLI checks; extract genuinely unique assertions and adapt to the current `evidence_refs` and renderer APIs.

The latest code already has evidence-reference regression tests. Do not duplicate legacy tests solely to increase the count.

## 4. Private-only items and why

| Class | Decision | Rationale |
| --- | --- | --- |
| Real source registry and playlist | Private only | Source mapping and privacy/scope control |
| Transcripts, captured media and frame boards | Private only | Reuse rights and source integrity |
| Actual research reports and V3.4 audits | Private only | Raw claims, semantic uncertainties and reviewer data |
| Raw benchmark of one historical media analysis | Private only | Actual video-derived evidence |
| Original Git history, old PR threads and diagnostic logs | Private only | May contain private identifiers, obsolete behavior and unsanitized context |
| Production signing or model credentials | Never commit | Credential leakage and future unauthorized execution |

The original private repository remains a reference archive, not a GitHub Actions dependency. No automatic Private→Public sync or credentials in Public CI.

## 5. Remaining work and release blockers

**Immediate:** preserve the public publisher block, restore offline CLI prerequisites, and retain evidence board renderer regression checks. Re-run both OS test matrices and the publication policy audit.

**Before public merge:** verify the existing offline security gate and Windows/Ubuntu regression/stress checks; review the migration changes. The personal maintainer has no current intention to issue MIT/Apache rights or ship binary distributions, so these are **not merge blockers**. Branch protection is recommended as a follow-up for accidental-direct-push prevention, not a required license gate. The private PR and artifacts remain untouched.

**Before Phase 5F live processing:** implement an offline-only worker adapter, immutable content-addressed file staging, atomic idempotent checkpoint settlement and explicit operator approval for any provider calls. An offline PASS is not a production approval.

This handoff is derived from historical code and safety notes; it does not reproduce transcripts, source IDs, actual frame contents or user-specific local paths.
