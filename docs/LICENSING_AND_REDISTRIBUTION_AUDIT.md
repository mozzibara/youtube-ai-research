# Licensing and redistribution audit — Phase 5E.1 public migration

**Audit date:** 2026-11-11  
**Scope:** `mozzibara/youtube-ai-research` Draft PR #1, baseline `9153d0a98318d55c8eff9a2f3bb4befde4594b1e`, three requirements manifests and Ubuntu Python 3.12 GitHub Actions installation log.  
**Decision:** **MIT recommended for verified first-party source code, but not yet granted. Binary redistribution is NOT APPROVED.** No root `LICENSE` file is added by this audit.

This is a source/dependency inventory, **not** a legal opinion or assurance that every transitive binary artifact has been reviewed. The conclusions apply to the explicitly observed versions only. Manifest ranges currently permit later versions.

## 1. Repository and ownership scope

- The public snapshot has **82 tracked files**, including Python source, tests, generic configuration, source-only documentation, and GitHub Actions. No tracked `research/`, `data/`, `cache/` or benchmark corpus was found by the current tracked-tree and public audit checks.
- No root `LICENSE` or third-party NOTICE bundle currently exists. GitHub Public visibility is **not** a general copyright license. A visitor can view/fork within GitHub's platform terms, but additional reuse permissions require an applicable license.
- A sample of the private source's 40 latest commits shows the `kimalswhd0001` author, and the Public migration's 16 commits show `mozzibara`. Git commit authorship is **not proof of exclusive copyright ownership**. The actual copyright owner must affirm that the code, prompts, docs and test snippets may be licensed and that no contractual/employment or third-party contributions constrain licensing.
- A targeted scan of 31 implementation Python files did not find explicit third-party copyright headers, SPDX identifiers or obvious copied-source headers. **This does not prove originality.** Perform a provenance review for nontrivial borrowed code, AI-generated snippets and the private source's older history before granting MIT/Apache rights.
- The original private repository and its research assets are not relicensed by adding a license to this Public source distribution.

## 2. MIT vs Apache-2.0

| Item | MIT | Apache-2.0 |
| --- | --- | --- |
| Commercial use, copying, modification, distribution | Allowed with copyright/license retention | Allowed with compliance with Apache sections |
| Source disclosure for independent modifications | Not required | Not required solely by Apache |
| Express contributor patent grant | No | Yes; includes patent-litigation termination condition |
| Mark modified files | No separate express change-marking clause | Required |
| Original notices | Retain copyright and license notice | Retain copyright/patent/trademark/attribution; `NOTICE` handling when present |
| Maintenance burden for this Python source project | Lower | Moderate |

**Recommendation:** MIT for first-party source because its notice requirement is straightforward and the current project does not have a documented patent-licensing need. Select Apache-2.0 instead if explicit inbound/outbound patent terms and change tracking are important. Neither choice grants permission to redistribute third-party GPL binaries under a more permissive license.

Sources: [SPDX MIT](https://spdx.org/licenses/MIT), [SPDX Apache-2.0](https://spdx.org/licenses/Apache-2.0), [GitHub licensing guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).

## 3. Actual Python CI dependency inventory

Installed by `requirements-dev.txt` → `requirements-multimodal.txt` → `requirements.txt` during [Ubuntu CI #42988775639](https://github.com/mozzibara/youtube-ai-research/actions/runs/42988775639). This list records **15 installed packages**, with 4 direct packages and 11 transitive/default-extra packages. License IDs are package/project-level declarations, not a complete license file inventory for each distributed wheel.

| Dependency | Resolved version | License found | Origin | Redistribution assessment |
| --- | --- | --- | --- | --- |
| `youtube-transcript-api` | 1.2.5 | MIT | Direct | Source/package license permissive; preserve notices |
| `av` (PyAV) | 17.1.0 | BSD-3-Clause **source**; bundled FFmpeg separate | Direct | **BLOCKER for redistributing wheel** without exact binary provenance |
| `Pillow` | 12.3.0 | MIT-CMU | Direct | Permissive; retain required notices and naming restrictions |
| `yt-dlp[default]` | 2026.10.26 | Unlicense for PyPI wheel/source | Direct | Code itself permissive; **default extra installs GPL Mutagen** |
| `brotli` | 1.2.1 | MIT | Transitive/extra | Preserve notice if repackaged |
| `certifi` | 2026.10.23 | MPL-2.0 | Transitive/extra | MPL notice and covered-file compliance if distributing/modifying |
| `charset-normalizer` | 3.5.2 | MIT | Transitive | Preserve notice if repackaged |
| `defusedxml` | 0.7.1 | PSF license | Transitive | Preserve applicable PSF notice |
| `idna` | 3.20 | BSD-3-Clause | Transitive | Preserve notice and non-endorsement terms |
| `mutagen` | 1.48.1 | **GPL-2.0-or-later** | `yt-dlp[default]` extra | **BLOCKER for a bundled distributor** pending GPL compliance strategy |
| `pycryptodomex` | 3.24.2 | BSD / public-domain portions | `yt-dlp[default]` extra | Verify per-component notices before redistribution |
| `requests` | 2.34.2 | Apache-2.0 | Transitive | Retain license/attributions and NOTICE if applicable |
| `urllib3` | 2.8.0 | MIT | Transitive | Preserve license |
| `websockets` | 17.4 | BSD-3-Clause | `yt-dlp[default]` extra | Preserve notices |
| `yt-dlp-ejs` | 0.8.0 | Unlicense AND MIT AND ISC | `yt-dlp[default]` extra | Preserve MIT/ISC notices for bundled components |

Primary evidence:
- [yt-dlp pyproject optional dependencies](https://github.com/yt-dlp/yt-dlp/blob/master/pyproject.toml) and [yt-dlp distribution licensing](https://github.com/yt-dlp/yt-dlp#licensing)
- [Mutagen GPL-2.0-or-later metadata](https://pypi.org/project/mutagen/1.48.1/)
- [PyAV 17.1.0 license](https://pyav.basswood.io/docs/17.1/development/license.html) and [install/bundled FFmpeg](https://pyav.basswood.io/docs/17.1/overview/installation.html)
- [PyAV wheel licensing discussion, issue #2270](https://github.com/PyAV-Org/PyAV/issues/2270) and [FFmpeg legal guide](https://ffmpeg.org/legal.html)
- [yt-dlp-ejs 0.8.0](https://pypi.org/project/yt-dlp-ejs/0.8.0/), [Pillow MIT-CMU](https://github.com/python-pillow/Pillow/blob/main/LICENSE), [certifi MPL-2.0](https://pypi.org/project/certifi/), [defusedxml](https://pypi.org/project/defusedxml/), [requests](https://pypi.org/project/requests/2.34.2/)

### Two material risks

**A. PyAV binary wheel != PyAV source.** PyAV declares BSD-3-Clause for its own source. Upstream's wheel-licensing discussion distinguishes bundled LGPL/GPL components and commercial exception claims. That thread concerns a family of wheels and does **not** independently establish the exact obligations for `av 17.1.0` wheels used here. Linux and Windows could differ. Before shipping an application image, obtain each wheel's exact filename/hash, bundled-library license notices, x264/x265 flags, FFmpeg build options and GPL/LGPL/commercial-exception documentary basis. If the conditions cannot be established, do not redistribute that binary.

**B. Mutagen GPL-2.0-or-later.** Installing `yt-dlp[default]` brought in the GPL package in actual CI. Merely referring to a GPL dependency in a separately licensed source project is not automatically proof that every line of that project becomes GPL. But bundling, linking, modifying or shipping a combined software product **can** have GPL obligations dependent on the distribution architecture. Avoid making a combined binary release until the legal/compliance route is established. A candidate mitigation is to depend on plain `yt-dlp` rather than the `[default]` extra *if functional tests prove optional features are not needed*, or to distribute components separately with appropriate terms. Do not silently remove dependencies without regression tests.

## 4. Three distinct redistribution scenarios

1. **Public source repository (current):** May license first-party source under MIT/Apache after confirming copyright ownership, contributor authority and code provenance. Third-party packages are installed by the end user/CI, not checked into this repository. Keep independent dependency license notices and provide a readable inventory.
2. **pip package / installer / Docker image / standalone .exe (not currently approved):** Re-evaluate *all actual included binary artifacts* for each OS/architecture. Record wheel SHA256, third-party notices, GPL/LGPL obligations and source-availability requirements. A repository-level MIT file does not override these.
3. **Video/media/transcript/AI response distribution:** **Not covered** by MIT/Apache licensing of the software. YouTube terms, rights-holder permissions, provider contracts, privacy and applicable legal exceptions require separate review. This Public repository deliberately excludes these assets and disables Git research publication.

YouTube terms: https://www.youtube.com/t/terms

## 5. Remaining approval gates

- [ ] Copyright holder or authorized representative verifies ownership and authority for *all* first-party source, tests, prompts and docs.
- [ ] Review original-source history and all imported snippets for third-party notices/licensing; resolve provenance of ambiguous code.
- [ ] Confirm final license choice (MIT recommended; Apache-2.0 alternative), correct copyright notice year/owner, and add **official unmodified license text** to root `LICENSE`.
- [ ] If redistributing dependencies, generate an OS/architecture-specific SBOM and exact wheel/hash inventory; preserve all notices and required license texts.
- [ ] Resolve `mutagen` GPL distribution architecture and exact PyAV FFmpeg binary license/exception status, or exclude these from redistributable bundles.
- [ ] Lock or otherwise record resolved package versions for repeatable audits; current open ranges allow dependency drift.
- [ ] Distinguish owner-authorized provider calls from YouTube content reuse. Never publish real research assets merely because source code is permissively licensed.
- [ ] Retain the Draft PR until licensing/redistribution and branch protection policy are accepted.

**Current disposition:** Source-license **recommended, pending owner rights confirmation**. **Binary redistribution NOT APPROVED**. Raw-media/AI-output redistribution NOT APPROVED. No license was granted or applied by this audit.
