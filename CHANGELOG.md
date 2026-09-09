# Changelog

All notable changes to `dx-orchestrator`.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.0] — 2026-09-09

### Added

- **`dx verify-gui` writes a `dx.gui_verification.v1` evidence bundle** — ROADMAP
  §1.3. The screenshot handed to the vision model is stored verbatim under
  `artifacts/screenshot.png`, beside the model's YES/NO answer, the expected
  text, and the model/endpoint. The whole point of the family: a later reader
  looks at the exact frame that was judged, not a re-capture or a one-line
  verdict. `sha256sum -c SHA256SUMS` covers the PNG, so a mutated screenshot
  fails verification — there is a test that it does.
- The bundle's `boundary` leads with RL-007: the model's answer is advisory
  evidence, never a proof, and `dx merge` still requires a GPG signature. The
  writer refuses an empty boundary here exactly as it does for `dx.role_task.v1`.
- Emission is on by default with the same controls as `dx run` —
  `--evidence-dir` / `DX_EVIDENCE_DIR` / `--no-evidence` — plus `--task` for the
  id it files under (default `verify-gui`). A failed check still leaves a bundle;
  an unwritable bundle fails closed, so a receipted verification that produced no
  receipt is reported as a failure rather than a clean pass.
- `evidence.py` grew a shared writer core (`_finalize`) so both families share
  the directory layout, `SHA256SUMS`, and the mandatory-boundary check, and it
  now supports binary artifacts. The test conftest redirects `DX_EVIDENCE_DIR`
  to a throwaway dir for every test, so no test can write into the real store.

### Still open (ROADMAP §1.3)

- The bundle closes the receipt half. PSOperator's observer is still not in the
  loop, so a signed observer envelope does not yet back the frame — that stays
  the remaining cap under §1.3 / §3.1.

## [0.11.0] — 2026-09-09

### Added

- **`dx verify-gui`'s VLM wait is configurable.** `DX_VLM_TIMEOUT`, or
  `gui_verification.timeout_s` in the manifest, overrides the 30 s default; the
  env var wins over the manifest. A large vision model on a cold load can take
  well over 30 s to answer, so a slow or memory-tight box was seeing every check
  fail with `VLM error: Read timed out` — a real box (an 8 GB M1) hit exactly
  this. The default is unchanged, so nothing already working changes. A
  non-numeric or non-positive value is a `GuiConfigError`, not a silent fallback.
  `TestVlmTimeout` pins the default, both override paths, the two rejections, and
  that the resolved value is the timeout actually handed to `requests.post`.

## [0.10.0] — 2026-09-08

### Added

- **`dx merge` performs the merge and appends to the ledger.** ROADMAP §1.2, the
  second and last of the deliberate pipeline stubs. `src/dx/ledger_writer.py`
  carries the ledger's invariants rather than the CLI's: append-only (RL-009),
  SCHEMA.md canonical form, and a `MERGE_LOCK.json` held for the duration.
  `dx merge <task> --repo <path>` performs `git merge --no-ff` of the queue
  file's `sha`; without `--repo` the approval is recorded and dx says plainly
  that it merged nothing.

  **The sequencing trap is handled, and tested.** Appending moves the head an
  approval binds to, so verification happens before any append and the head is
  re-read between the `SIGNED` and `MERGED` rows. Reusing the pre-append head
  writes a row one behind and breaks the chain; `append_row` refuses it, and
  `TestTheSequencingTrap` proves the refusal. Run `dx merge` twice and the second
  run is correctly rejected as stale — that is the mechanism working.

  **`--force` writes nothing.** It bypasses the gates, so dx will not append
  rows: the ledger would otherwise attest to a check that did not happen. Its
  message says so instead of the old "stub" text.

### Fixed

- **A test was writing to the operator's real ledger.**
  `test_the_green_merge_transcript_is_reproducible` shells out to `dx merge`,
  which resolves to `~/ai/devswarm-ledger-reference` when `DX_LEDGER_REPO` is
  unset. That was harmless while merge only read; the moment it grew teeth,
  every suite run appended `SIGNED`, `MERGED` and `UNLOCK` commits to the
  working clone. Restored from `origin/main` — nothing was pushed, so the
  published ledger was never affected. Transcript tests now run against a
  disposable copy, and a full suite leaves that repository byte-identical.

  The lesson generalises: a test that was safe against a read-only command
  becomes a mutation when the command changes, and nothing about the test has to
  change for that to happen.

- **`fake_ledger` was a fiction that only worked while nothing wrote.** Its stub
  `verify_chain.py` printed a head unrelated to its own rows, which carried no
  `prev_hash` at all. `append_row` cross-checks the verifier's claimed head
  against the actual last row and refused it — correctly. The fixture is now a
  real chain in a real git repo, with the head computed from the rows.

- `TestCanonicalFormMatchesTheVerifier` imported `verify_chain.py` from the
  operator's clone, leaving `__pycache__` in it. It copies the file out now: a
  test has no business writing anything into the ledger repository.

## [0.9.1] — 2026-09-08

### Fixed

Two defects in the 0.9.0 evidence bundles, both found by using `dx` to build
`dx` — neither was visible from reading the code, and the suite was green
through both.

- **A run that changed nothing was recorded as a success.** The first factory
  run reported `COMPLETED` after 11 rounds and 198k tokens without writing a
  file. pxx exited zero, so `result.passed` was `True` and every check was
  green — an empty run reading as an accomplishment. Bundles now carry a
  `produced_changes` check. A no-op is recorded, not failed: some tasks
  legitimately change nothing. It must simply be visible in its own receipt.
- **`produced_changes` then reported a real run as zero changes.** It was
  derived from `git diff <base>`, which shows *tracked* changes only — so a
  brand-new file, the most common shape of a successful task, was invisible to
  it. It now comes from `git status`, which sees untracked paths, and the
  `boundary` block says the patch covers tracked changes only.

The second is the same mistake as the first: deriving "did anything happen" from
a signal that cannot see the answer.

### Added

- `docs/admissions/T-1102-ledger-append.md` — the admission record for ROADMAP
  §1.2, drafted by `dx run --required_role tech-lead` against the lab vLLM node
  and accepted after review. Its §4 independently states "stop and open a new
  admission rather than widening this one" — the discipline T-1101 broke.


#### Also in 0.9.1 — from a second session, landed 2026-09-09

The `verify-gui` capture fix below shipped on `main` under the same version number
while the evidence-bundle work above was in flight on a branch; both are in the
0.9.1 tree, so both are recorded here.

### Fixed

- **The default `screenshot_cmd` did not produce an image.** `import -window
  root -` makes ImageMagick write **PostScript** to stdout, not PNG, and nothing
  checked — so `dx verify-gui` base64-encoded a PS document and sent it to the
  vision model as a screenshot. Every manifest seeded by
  `setup_dependencies.sh`, the tutorial's example and the module default all
  carried it. The default is now `import -window root png:-`, which forces the
  format. **If your manifest was seeded before 0.9.1, change it** — the old
  string is now rejected loudly rather than forwarded silently.

### Added

- `_capture_ssh` checks the PNG magic bytes and fails with a `capture error`
  naming the fix. `TestCaptureMustBePng` pins both directions: PostScript is
  rejected with the hint, and a real PNG reaches the VLM unchanged.

### Verified

- `dx verify-gui` has now run against a live desktop for the first time: an
  Xvfb + Openbox session on a headless Linux box, captured over SSH, checked by
  a local vision model. A matching expectation returned YES in under five
  seconds; a mismatched one returned NO. That is the SSH-capture path only —
  ROADMAP 1.3 remains open because no `dx.gui_verification.v1` bundle is
  written and PSOperator's observer was not involved. Two operational notes
  from that run: the VLM call is capped at 30 s, so a cold model that takes
  longer to load fails with `Read timed out` until it is warmed; and macOS
  refuses screen reads from SSH-spawned processes until the sshd wrapper is
  granted Screen Recording, so a Mac needs that one-time approval before it can
  be an `ssh_host`.

## [0.9.0] — 2026-09-08

### Added

- **`dx run` writes `dx.role_task.v1` evidence bundles.** ROADMAP §1.1, the
  first of the two deliberate stubs to close. A bundle is a directory —
  `README.md` for a human, `manifest.json` for a machine, `SHA256SUMS` for
  tamper-evidence, and artifacts beside them: the injected prompt, the resolved
  endpoint/model/provider, the scope diff, git status. Verification is
  `sha256sum -c SHA256SUMS` on any POSIX box with no Python and no network.

  Shape comes from `VISION.md § Reference formats`, drawn from surveying 111
  real bundles in `cdnwetzel/camelid`. It was implemented, not redesigned.

  Bundles land in `~/.local/state/dx/evidence` by default — deliberately outside
  the repository under edit, so receipts never end up in the tree pxx is
  committing. `--evidence-dir` and `DX_EVIDENCE_DIR` override; `--no-evidence`
  skips emission, and skips the git probe with it so it genuinely costs nothing.

  Three decisions worth naming:

  - **Evidence must not perturb what it observes.** pxx's stdout is *not*
    captured. `cmd_run` hands pxx the inherited fd deliberately, and interposing
    a pipe changes what pxx sees — the same class of mistake as measuring a
    circuit with a meter that loads it. The `boundary` block says the transcript
    is absent rather than pretending otherwise.
  - **Failed runs get bundles too.** A store that only records successes is a
    highlight reel.
  - **An unwritable receipt fails the run closed**, at `EXIT_ERROR` — not
    `EXIT_TASK_FAILED`, which would report a task that succeeded as one that
    failed. A receipted run that produced no receipt is not a receipted run.

- **`boundary` is enforced, not documented.** `write_bundle` refuses to emit a
  bundle whose boundary block is empty, and leaves nothing on disk when it
  refuses. What a bundle claims is a `compliance-privacy` judgement and that
  card is Anchored, so the wording is policy text carrying a comment saying so.

- `docs/admissions/T-1101-evidence-bundles.md` — the task admission record this
  work was done under, issued per the `tech-lead` card's seven-point format.
  `backend-engineer` requires one as an input and there wasn't one; its absence
  is why §1.1 was described rather than executable. First time dx's own
  development has been governed by the deck dx enforces.

### Fixed

- Inserting the evidence hook split the `# unbounded:` justification from the
  `subprocess.run` it annotates, and `test_every_subprocess_run_in_the_package_is_bounded`
  caught it. The comment is back against its call. A justification that has
  drifted away from the thing it justifies is worse than none — it reads as
  covering the wrong line.

## [0.8.1] — 2026-09-08

### Fixed

- **`TUTORIAL.md` §7 told readers to run a command that fails.** It showed
  `dx merge T-0007` — a task that exists only in a private operational ledger.
  Once `0.7.0` repointed `DEFAULT_LEDGER_REPO` to the public reference ledger,
  every new reader got `queue file not found` from the section demonstrating the
  project's central gate. §7 now uses `T-0001`, and shows the transcript that
  release made possible: an **all-green merge**, four checks, exit 0.
- **§6 quoted a superseded failure line and the wrong exit code.** It showed
  `❌ pxx task failed.` and "exit code 2" — both changed by `0.7.1`, which moved
  a failed task to `3` precisely so it could not be mistaken for the Anchored
  refusal that owns `2`. A tutorial quoting the old line teaches the old
  contract.
- §7 claimed the fully-green happy path was "still not demonstrated live". It
  is, now, against the reference ledger with a real `gpg --verify`. The boundary
  was rewritten to say what is actually still missing: the RL-010 *ceremony* — a
  signature made interactively, over a ledger whose rows attest to real work.

### Added

- `TestMergeTranscriptFidelity` — §7's transcripts are re-run and required to
  match, and the task id shown must exist in the ledger `dx` defaults to. §6's
  failure line and exit code are pinned to what `cmd_run.py` emits. Neither
  section was checked before, which is why both broke silently under 337 green
  tests.
- CI clones `devswarm-ledger-reference` as well as the role deck, so those
  guards run rather than skip.

### Changed

- `checkpoint.md` rewritten as a resume document again: the go-live block was
  still headed `v0.6.2`, its evidence table cited a test count four releases old,
  and four session narratives had accumulated below it. Detail belongs here in
  `CHANGELOG.md`.
- `RELEASE_READINESS.md` marked as a historical record and its repo table
  corrected — it still told readers the ledger was private and blocked.
- The README's first screen said `dx` routes to "whatever speaks vLLM or Ollama".
  It speaks anything OpenAI-compatible; the same undersell was fixed lower down
  in 0.8.0 and missed here.

## [0.8.0] — 2026-09-08

### Added

- **`openai-compatible` is documented, with a provider table.** `pxx` has always
  accepted it — and treats an unrecognised provider as OpenAI-compatible rather
  than failing — but `dx`'s README listed only `ollama | vllm | openai`. Anyone
  running llama.cpp, LM Studio, TGI, LiteLLM or a hosted API read that list and
  concluded `dx` was lab-specific. It is not: wiring it to another inference
  stack is a manifest edit, not a code change, and routing is per-role so
  different roles can sit on different backends. Verified end-to-end against a
  generic `openai-compatible` route.
- The README now says `PXX_API_KEY` reaches `pxx` (`dx` passes the environment
  through untouched), so authenticated endpoints need no `dx` change.

### Changed

- **A trailing `/v1` on an endpoint is now corrected instead of merely warned
  about.** `pxx` appends its own suffix, so `.../v1` was probed as
  `/v1/v1/models`, 404'd, and failed every task with `MODEL_UNAVAILABLE`. It was
  documented in four places — tutorial, troubleshooting row, manifest comment,
  doctor warning — and documenting a footgun four times had not removed it,
  because hosted OpenAI-compatible services publish their base URL *with* the
  `/v1`. Pasting the vendor's own string is the common case, not carelessness.

  `normalize_endpoint()` strips trailing `/v1` segments and `dx run` announces
  the change on stderr; `RoleRoute.endpoint_raw` carries the original so the
  correction is never silent. `dx doctor` still flags the manifest so the file
  ends up saying what actually runs.

  Parsed, not string-suffixed: `http://v1` ends with the characters `/v1` while
  its path is empty and its *host* is `v1`, and a naive `endswith` strip turns
  it into `http:/` — a broken endpoint from the function meant to unbreak them.
  There is a test for exactly that, and the whole set was checked by reverting
  to the naive implementation and watching four cases fail.

## [0.7.2] — 2026-09-08

### Fixed

- **A real lab address reached a tracked file.** `VISION.md`'s red line is "no
  real lab addresses in tracked files" and the go-live claim was "0 lab
  addresses in the tree **or** the full git history". The guard enforcing it
  (`test_no_real_addresses_leaked_back_in`) only ever read `TUTORIAL.md`, so
  writing one into `checkpoint.md` — while documenting the fix for exactly this
  problem in `psoperator` — passed 327 green tests and was pushed as `e066854`.

  Scrubbed from the tree. It cannot be removed from history without a
  force-push, which the branch ruleset now forbids and which is the wrong trade
  on a published repo, so the privacy claim in `checkpoint.md` is scoped to say
  the tree is clean and the history is not.

### Added

- `TestNoLabAddressesAnywhere` — the red line is now enforced across **every
  tracked text file**, not one document. RFC1918 and RFC6598 are rejected;
  loopback and the RFC-5737 documentation ranges are allowed, because those are
  what examples should use. Reports `file:line`, and carries a self-test so a
  pattern that silently stops matching fails the build.

  The narrow guard was the actual defect. A red line enforced on one file is a
  red line that reads as enforced and is not.

## [0.7.1] — 2026-09-08

### Fixed

- **`dx run` let pxx forge an Anchored refusal.** It returned pxx's exit code
  verbatim, and `2` is `dx`'s code for "governance refused this role". pxx exits
  2 in the wild — a missing shell safeguard does it — so a caller could read
  exit 2 and conclude policy had refused the run when in fact the run was
  allowed and the work failed. Those demand opposite responses: "you are not
  permitted to run this" versus "fix your task".

  A downstream failure is now `3`, and pxx's real code moves into the message
  (`❌ pxx task failed (pxx exit 2).`) rather than being returned. Exit codes are
  named constants in `cmd_run.py`; `2` is reserved for `dx`'s own decision and
  is no longer reachable from a subprocess.

  Found by running a real `dx run` end-to-end after the 0.7.0 rename, not by
  reading the code.

### Added

- `TestExitCodeContract` in `tests/test_docs_consistency.py` — the README's
  exit-code line is now machine-checked against the constants in both
  directions (no undocumented code, no documented code the CLI cannot produce),
  plus a guard that fails the build if `cmd_run` ever returns a subprocess's
  exit code again. Each guard was verified to fire by reintroducing the defect.

## [0.7.0] — 2026-09-08

### Changed

- **Every dependency is now a public, MIT-licensed repository.** The role deck
  moved to `sdlc-agent-roles` (public, MIT — the superset that replaced the
  archived `claude-sdlc-roles`), and `dx merge` now defaults to
  `devswarm-ledger-reference`, a public reference ledger carrying synthetic rows
  and one real GPG signature. The live operational ledger stays private and is
  reached by setting `DX_LEDGER_REPO`.

  Defaults changed accordingly, which is breaking for anyone relying on them:

  | | Was | Now |
  | --- | --- | --- |
  | `DEFAULT_ROLES_PATH` | `~/ai/claude-sdlc-roles/skills/sdlc-role/roles` | `~/ai/sdlc-agent-roles/skills/sdlc-role/roles` |
  | `DEFAULT_LEDGER_REPO` | `~/ai/devswarm-ledger` | `~/ai/devswarm-ledger-reference` |

  Both remain overridable by `DX_ROLES_PATH` and `DX_LEDGER_REPO`.

### Removed

- **The `gh auth login` requirement.** `setup_dependencies.sh` no longer
  preflights the GitHub CLI or branches its clones through `gh repo clone` —
  nothing it fetches is private any more. `gh` is no longer a prerequisite
  anywhere in the install path.
- **The README's "Before you clone" section**, added one release ago in 0.6.2 to
  warn visitors that two integrations were private repos. Its reason to exist is
  gone. In its place, a "Reproducing this" section naming all five repos and how
  each is obtained.

### Fixed

- The hermetic-guard test's rationale said "private sibling repo". The repos are
  public now, so the rule was restated around what it actually protects: the
  README's claim that `pytest` runs with nothing but this repository. The rule
  itself is unchanged — a test that hard-requires a clone still needs a skip
  guard.

## [0.6.2] — 2026-09-08

### Added

- **A "Before you clone" section at the top of the README.** Two of the four
  integrations — `claude-sdlc-roles` and `devswarm-ledger` — are private repos,
  and that fact was buried in the Dependencies section. A visitor would follow
  the quick start and meet it as a 404. It is now the first thing after the
  intro, with a table of what does and does not work without them, and a
  pointer to the test suite as the surface built to be evaluated from outside.
- The README's test count is now checked against the real collected count, so
  a number a reviewer would reasonably trust cannot quietly drift.

## [0.6.1] — 2026-09-08

Go-live pass. Every claim in `TUTORIAL.md` was re-run against the real lab and
checked against actual output rather than assumed.

### Fixed

- **`dx run`'s status line printed after the output of the command it
  announces.** `print()` buffers when stdout is not a tty, while pxx writes
  straight to the inherited descriptor, so a piped or redirected run transcript
  read out of order. Same defect fixed in `dx merge` in 0.3.0; a run transcript
  is evidence too. All of `cmd_run`'s status output is now flushed.
- **`TUTORIAL.md` §6 contradicted its own instructions.** It told the reader to
  `export PXX_ALLOW_UNGATED_SHELL=1`, then showed a transcript captured without
  it — a failing run, exit 2. A reader following the steps literally gets a
  *successful* run with different output. The success path is now the primary
  transcript, with the `[HOOKS_MISSING]` failure kept as the documented
  what-if-you-skipped-it case.
- **`TUTORIAL.md` §5 showed output the command cannot produce.** The
  `dx roles list --fit High` block had column widths from before the
  hardcoded-width fix; widths are computed from the filtered data, so that
  command has never printed that table. Recaptured from a real run.
- The tutorial's `dx --version` comment still said 0.3.0, and its closing
  boundary paragraph still said the merge gate had only ever passed all-green
  against a *stubbed* signature — untrue since 0.4.0 added real-key coverage.

### Added

- `TestTutorialFidelity` in `tests/test_docs_consistency.py`. It re-runs
  `dx roles list --fit High` and fails the build if the shown transcript drifts,
  checks the version comment against the package, and asserts the address
  substitution stays declared and the boundary paragraph stays present. The
  tutorial's worth rests on "every output was captured from a real session", and
  that claim had already decayed once without anyone noticing.

## [0.6.0] — 2026-09-08

Two more fail-opens, both found by feeding odd input to commands rather than
reading them. Both are the same shape: two parts of dx disagreeing about
whether an install is usable, with the permissive one winning.

### Security

- **`dx run` executed under a role card that fails validation.** A card with a
  too-short mandate and no "Must not" section was accepted, and dx injected an
  empty `MANDATE` and an empty `MUST NOT` into the prompt and reported success.
  The governance text that is supposed to constrain the agent was silently
  blank — while `dx roles validate` had been calling that same card invalid all
  along. `dx run` now validates the card before using it and refuses, listing
  the specific failures; `--force` bypasses and says so on stderr.
- **`dx doctor` reported a healthy install with zero role cards.** No
  constitution at all passed the self-test, while `dx roles validate` correctly
  failed on the same directory.

### Changed

- **`dx run` now exits 1 for a role card that fails structural validation**,
  where it previously proceeded. A behavior change for anyone running against
  hand-edited or partial cards.
- `dx doctor` fails when the role-card directory is empty.

## [0.5.1] — 2026-09-08

### Fixed

- **`dx merge` reported a malformed approval payload as a stale signature.** Any
  payload of the form `<task_id><anything><role>` was diagnosed as "Stale
  signature (RL-003) — re-sign after re-verifying the chain", including payloads
  whose middle section was empty, non-hex, or the wrong length. Every such
  payload was correctly *rejected*; the problem was the advice. "Stale" tells an
  operator to re-sign against the current head, which is the wrong remedy for a
  tampered file and quietly launders an alteration into a fresh valid signature.

  Staleness is now claimed only when the middle section is a 64-character
  lowercase hex ledger head. Anything else reports a malformed payload, names
  the file, and says explicitly not to re-sign it. Unrecognisable payloads
  report the expected and actual byte lengths.

## [0.5.0] — 2026-09-08

Two fail-open defects in the governance layer, and the first check for the
footgun this project documents most.

### Security

- **An unparseable role card was silently dropped, and `dx roles validate` still
  reported `PASS`.** The role cards are the constitution; validate is the
  command that certifies it is intact. It printed a `WARN` to stdout, excluded
  the card, counted the rest, and exited 0 — certifying a deck with pages
  missing.

  The sharp end is Anchored roles. Those seven exist to refuse autonomous
  execution. A corrupt Anchored card vanished from the registry entirely, so
  `dx run --required_role <that role>` reported "role not found" and the
  hard-block never fired — a fail-open on the one gate whose entire job is to
  stop. Parse failures are now recorded, fail `dx roles validate`, fail
  `dx doctor`, and `dx run` distinguishes "that card is corrupt" from "no such
  role" (the latter reads like a typo and sends you looking in the wrong place).

### Added

- `config_loader.endpoint_warnings()` and a `dx doctor` warning for the
  **trailing `/v1`** mistake. It has a tutorial section, a troubleshooting row,
  and a comment in the seeded manifest — and nothing checked for it. pxx appends
  its own suffix per provider, so `http://host:8000/v1` is probed as
  `/v1/v1/models`, 404s, and every task fails with `MODEL_UNAVAILABLE`.
  Documenting a footgun is not the same as removing it. Endpoints missing a
  URL scheme are flagged too, and a test asserts the shipped seed manifest does
  not demonstrate the mistake it warns about.
- `role_registry.get_parse_failures()` and `failed_slug()`.
- `tests/test_corrupt_role_cards.py`.

### Changed

- **`dx roles validate` now exits 1 when any card fails to parse**, where it
  previously exited 0. This is the point of the release, but it is a behavior
  change for anything scripting that exit code.
- `dx doctor` parses every role card rather than counting `*.md` files, and
  reports "Role cards parse cleanly" instead of "Role cards found".

## [0.4.1] — 2026-09-08

Malformed operator input now stops the line with a message instead of a
traceback. All three defects below were found by probing the code with bad input
rather than reading it.

### Fixed

- **`dx merge` crashed on a corrupt ledger — after printing two green
  checkmarks.** A `ledger.jsonl` row that was not valid JSON raised a bare
  `JSONDecodeError` from inside the gate, past the chain and signature checks
  that had already reported success. That is the worst available shape for a
  gate failure: it looks like it is passing, then explodes. Corrupt rows now
  raise `LedgerError` naming the file and line number and invoking RL-009.
- **`dx run` printed a Python traceback for a hand-edited manifest.** Writing
  `roles:` as a list — the usual way this mistake is made — produced
  `AttributeError: 'list' object has no attribute 'get'`. The manifest is
  hand-edited by design, so a wrong shape is an ordinary operator mistake. It
  now raises `ConfigError` naming the file, the offending key, and the likely
  cause.
- **`dx doctor` reported "All core checks passed" for a manifest `dx run` could
  not use.** It validated YAML *syntax* and stopped there, so a structurally
  wrong manifest passed the self-test and failed on first use. `dx doctor` now
  loads the manifest exactly the way the commands do, and `validate_manifest()`
  checks **every** role entry rather than only the one a given command happens
  to select — validating just the default route let a malformed entry for
  another role through.
- **Unbounded subprocess calls.** `psoperator observer-health`, `audit-verify`
  and `kill` had no timeout; an emergency stop that can hang is not an emergency
  stop. `dx doctor`'s version probes and the chain verifier and gpg calls are
  bounded too. `dx run`'s call into pxx is deliberately left unbounded — that is
  the model doing the work — and carries a comment saying so.
- A `queue/<task>.json` containing a JSON array was returned as-is and failed
  later on `.get("approve_role")`, far from the cause.

### Added

- `ConfigError` for malformed manifests, and a top-level handler in `cli.main()`
  that turns both it and `LedgerError` into one line and exit 1. Programming
  errors still surface as tracebacks — only these two operator-input families
  are caught, and a test pins that distinction.
- `config_loader.validate_manifest()` — checks every section and every role.
- `tests/test_malformed_inputs.py`, including an audit that fails the build if
  any `subprocess.run` in the package lacks a timeout or a documented
  `# unbounded:` reason, plus a vacuity check so the audit cannot pass by
  matching nothing.

## [0.4.0] — 2026-09-08

Peer-review hardening. The 0.3.0 release fixed three gates that were failing
open; this one raises the floor so the next three are caught by machine rather
than by reading. Coverage 70% → 91%, `mypy --strict` clean, and the RL-003
signature gate is now proved against real GPG keys instead of captured text.

### Added

- **Real-key GPG integration tests** (`tests/test_gpg_integration.py`). Committed
  public keys and detached signatures for a usable, a revoked and an expired
  ed25519 key. The suite asserts what previously had only been reasoned about:
  `gpg --verify` **exits 0 and emits `VALIDSIG` for signatures made by revoked
  and expired keys**, so the pre-0.3.0 condition (`rc == 0` plus `VALIDSIG`)
  cannot distinguish a usable key from a retired one. It also runs the complete
  `dx merge` gate all-green against a real signature — the path that had never
  been exercised end to end.
- `tests/fixtures/gpg/regenerate.py` and a README explaining the fixtures, so
  they are reproducible rather than opaque blobs.
- **Documentation consistency tests** (`tests/test_docs_consistency.py`).
  Versions must agree across `pyproject.toml`, `dx/__init__.py` and this file;
  every environment override must be documented exactly as implemented, in both
  directions; every subcommand must appear in the README table; and no routable
  IP literal may appear in the package.
- Tests for `cmd_doctor` (11% → 82%), `psoperator_client` (24% → 100%), the
  `dx run --gui` pipeline, the VLM HTTP call, and the CLI entry point.
- `SECURITY.md` — trust boundaries, the disclosure route, and an explicit list of
  what `dx` does *not* protect against.
- `CONTRIBUTING.md` — the gates, the fixture rules, and the one standing rule:
  a gate without a test is a claim, not a gate.
- `mypy --strict` across the package, and a CI job enforcing it.
- CI now fails below 88% coverage, and fails if the GPG integration tests skip
  rather than run.

### Fixed

- **`prohibited_patterns` returned fragments, not prohibitions.** Every line of
  a Markdown bullet list was treated as its own item, so a wrapped prohibition
  became two entries — a real card's four-item "Must not" section yielded six,
  two of them sentence tails like `'argue past it.'` — while emphasis markers
  were half-consumed, leaving stray `**`. Continuation lines are now folded into
  their bullet and emphasis is stripped cleanly. This field exists to be matched
  against agent behavior, so partial sentences in it were a governance defect.
- **`cmd_doctor` hardcoded `~/ai/psoperator`** while `psoperator_client`
  honoured `PSOPERATOR_REPO`, so the two could disagree about the same install.
  Both now share `get_psoperator_repo()`, resolved per call rather than frozen
  at import.
- Six `PSOPERATOR_*` environment overrides were read but undocumented; the
  README table now matches the implementation, and a test keeps it that way.
- `pytest -q` produced no summary line, because `addopts` already supplied `-q`
  and the two combined to `-qq`.

### Changed

- All `register_*_subcommand` and `cmd_*` functions carry real argparse types
  via a shared `SubParsers` alias; `dict`/`list`/`CompletedProcess` annotations
  are parameterised.
- `cmd_doctor`'s six deferred function-local imports are hoisted to module
  scope — there was no circular dependency justifying them.
- `dev` extra now includes `pytest-cov`, `mypy` and the type stubs.

## [0.3.0] — 2026-09-07

The repository-solidification release: a licence, a test suite, CI, and the
defects that writing the test suite exposed.

### Security

- **`dx merge` no longer accepts signatures from revoked or expired keys.**
  `gpg --verify` exits 0 and emits `VALIDSIG` for a signature made by a revoked
  or expired key, so gating on the return code let a retired approval key clear
  the RL-003 check. Verification now requires `GOODSIG` and explicitly rejects
  `REVKEYSIG`, `KEYREVOKED`, `EXPKEYSIG`, `KEYEXPIRED`, `EXPSIG` and
  `SIGEXPIRED`, naming the reason in the error.
- **Separation-of-duties enforcement no longer fails open.**
  `SignerIdentity.name` stripped only a `(comment)` from the GPG uid, so a key
  with no comment field kept its `<email>` attached. The comparison against the
  ledger's `author_human` could then never match and an author could approve
  their own task. The name is now reduced to the bare name in every uid form.
- **No hardcoded remote hosts.** `cmd_verify.py` shipped a real lab IP and SSH
  username as fallback defaults, so an unconfigured install would silently SSH
  to another machine. An unset `gui_verification` section is now an error that
  names the manifest it read.
- The manifest seeded by `scripts/setup_dependencies.sh` now contains
  placeholder hosts instead of a real lab topology.

### Added

- `LICENSE` (MIT) — previously claimed in `pyproject.toml` and `README.md` with
  no file present, which left the code all-rights-reserved.
- `tests/` — 146 tests covering role parsing, structural validation, hardware
  routing, the Anchored hard-block, the RL-003 gate (including the all-green
  path), the GPG accept/reject policy, and GUI configuration resolution. The
  suite is hermetic: synthetic fixtures, no private sibling repos, no keyring.
- `.github/workflows/ci.yml` — ruff, pytest on Python 3.11/3.12/3.13, package
  build, and a check that `LICENSE` ships inside the wheel.
- `dx --version`.
- `DX_ROLES_PATH` environment variable and an optional `roles_path:` manifest
  key. The role-card directory was the one external path with no override.
- `DX_VLM_ENDPOINT`, `DX_GUI_SSH_HOST` and `PSOPERATOR_SNAPSHOT_DIR` overrides.
- `RESOURCES.md` — control-plane footprint, host tooling, and per-role-class
  fleet sizing requirements.
- `CHANGELOG.md`.

### Fixed

- **`dx merge` gate verdicts printed out of order when piped.** Failures go to
  stderr (unbuffered) and passes to stdout (block-buffered off a tty), so a
  redirected transcript listed the failure before the checks that preceded it.
  A gate transcript is evidence; every verdict is now flushed as it is decided.
- **`dx run --force` was documented as audit-visible but printed nothing** when
  it bypassed an Anchored role. It now emits a banner naming the role and seat,
  matching `dx merge --force`.
- **`dx merge --force` under-reported what it skips.** It also bypasses GUI
  verification; the help text and banner now say so.
- Manifest and role-registry caches were keyed on nothing, so changing
  `DX_CONFIG` or loading a second role directory in one process silently
  returned the previously loaded data. Both caches are now keyed on their path.
- A role entry specifying only `endpoint` now inherits `model` and `provider`
  from `default` instead of dropping them.
- `PSOperatorClient`'s docstring described an invocation form
  (`python -m examples.run_agent`) that was replaced by an absolute-path call.

### Changed

- `pyproject.toml`: PEP 639 licence metadata (`license = "MIT"` +
  `license-files`), project URLs, trove classifiers, ruff and pytest
  configuration, `ruff` added to the `dev` extra.
- `dx doctor` reports the manifest path it actually resolved rather than a
  hardcoded one, and hints at `DX_ROLES_PATH` when role cards are missing.

## [0.2.0] — 2026-09-07

### Added

- `dx merge` implements the RL-003 signature gate against
  `cdnwetzel/devswarm-ledger`: chain verification via the repo's own
  `tools/verify_chain.py`, detached-signature verification against keys in
  `docs/keys/` using a scratch keyring, payload binding to
  `task_id + ledger_head + role`, and a signer ≠ `author_human` check.
- `ledger_utils.py` — thin helpers over the ledger repo; nothing the ledger
  already ships is reimplemented.
- Anchored roles are hard-blocked in `dx run` (exit 2) with the card's handoff
  text; `--force` bypasses.
- Per-role `PXX_MODEL` and `PXX_PROVIDER` routing alongside `PXX_BASE_URL`.
- `setup_dependencies.sh` preflights an active virtualenv and `gh` auth.

### Fixed

- Manifest endpoints carrying a trailing `/v1` produced `/v1/v1/models` probes
  and a 404 on every task; endpoints are now bare and `provider` selects the
  suffix.
- `dx doctor` network probes switched from `curl -f` (which flags vLLM's
  404-on-`/` as a failure) to raw TCP connect, and are derived from the
  manifest rather than hardcoded.

## [0.1.0] — 2026-09-07

### Added

- Initial control plane: `dx doctor`, `dx roles list|validate`, `dx run`,
  `dx merge`, `dx verify-gui`.
- Role-card parser for the `claude-sdlc-roles` format, structural validator,
  and hardware routing from `~/.config/dx/hardware_manifest.yml`.
- `scripts/setup_dependencies.sh`, `README.md`, `VISION.md`, `checkpoint.md`.

[0.12.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cdnwetzel/dx-orchestrator/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cdnwetzel/dx-orchestrator/releases/tag/v0.1.0
