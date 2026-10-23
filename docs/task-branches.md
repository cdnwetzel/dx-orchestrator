# Task branches: nothing reaches the base branch before a human merges it

**Status:** implemented in 0.23.0 (WP-3 of the PS Coding program), decided by
plan D-3 (2026-10-08). **Design:** AskPS `docs/general/ps-coding-wp3-design.md`
(Kimi design review 2026-10-12, PASS).

## The problem it closes

Through 0.22.0 `dx run` handed pxx `--commit` and pxx committed on whatever
branch the scope had checked out — the pilot's `main`. The work was on `main`
before anyone reviewed or signed it. `dx merge` then found the candidate
already reachable, `git merge --no-ff` said "already up to date", and the
MERGED row recorded a merge that never happened (AskPS finding F-003; T-0071
reached `main` this way). The signature was real; the gate was not.

## The choice lives in the manifest

```yaml
# ~/.config/dx/hardware_manifest.yml  (and config/manifest.template.yml)
branches:
  enabled: true        # no default; a typo must not become "inline"
  base: main           # the branch runs start from and merges land on
  prefix: task/        # task branch = prefix + task id
```

`dx.config_loader.get_branches_config()` reads it the way `get_review_config`
reads its section: absent → `None` (inline mode, 0.22.0's behaviour); present
but malformed → `ConfigError`, so `dx doctor` fails on it before the first run.
`base` and `prefix` are checked against what git refuses in a ref name, and a
base inside the prefix is refused (it would read as a stray task branch).

`dx run --task-branch` **asserts** the mode; it does not enable it. With
`enabled: true` the run is in branch mode with or without the flag. With the
section absent or `enabled: false` the flag is refused: "the manifest says
inline; this run was launched expecting a task branch". A launcher that passes
the flag therefore never gets a silent inline run from a manifest that drifted.

## What `dx run` does in branch mode

Before pxx starts, each step fail-closed with nothing written:

1. The scope is a git repository, its checkout is on `base`, and the tree is
   clean (`git status --porcelain` empty). A checkout found on a task branch
   is refused naming the branch: a previous run did not return it.
2. The task's `base_sha` is read from `queue/<task>.json` in the ledger repo
   and must be a commit in the scope. This is the one place the branch point
   comes from.
3. `git switch -c <prefix><task id> <base_sha>`. An existing branch of that
   name refuses the run.
4. The seed, if any, is committed on the new branch (`--seed-patch`,
   unchanged). A seed that does not apply refuses the run; the empty branch
   goes and the checkout returns.
5. pxx runs exactly as before. Its safety net (stash, `pxx-pre/<ts>` tag,
   reset on a non-COMPLETED outcome) is branch-independent, and dx checks out
   nothing while pxx runs.

After pxx returns:

6. If HEAD moved past the commit pxx started from, the candidate is tagged
   `candidate/<task id>` (`-2`, `-3`… when a re-admitted id already has one).
   The tag is read back and must resolve to the commit. **A tag failure is
   fail-closed:** no EXECUTED row, the branch is kept, the run exits `4`.
   Tagging is not conditioned on pxx's exit: a commit made before a non-zero
   exit is still work, and deleting its branch would orphan it.
7. If the tree is clean, `git switch <base>` and `git branch -D <branch>`.
   The candidate is held by its tag; a failed run's branch held nothing, or
   only a seed commit that is reproducible from the predecessor's bundle.
8. If the tree is dirty — the governance-scan case, pxx's own restore
   failing, `--no-commit`, or work salvaged from a refused run — the checkout
   stays on the task branch. dx prints exactly that with the branch name and
   the two commands that return it, and exits `4` (`EXIT_BRANCH_NOT_RESTORED`),
   distinct from `3`: the work may be good, and an EXECUTED row may even have
   been written. Nothing is reset. The next branch-mode run is refused by
   step 1 until a person returns the checkout.

Then the evidence bundle is written and, when pxx exited zero with a tagged
commit, EXECUTED is recorded with that commit as `sha` (the checkout is back
on the base by now, so the sha is passed, never read from HEAD) and the
control-plane check runs as before (a REDLINE row; the candidate stays on its
tag, `main` untouched).

The order matters and is deliberate: the bundle's `branch.json` has to say
whether the checkout was restored, so the return path runs before the bundle
is written; and the bundle is written before EXECUTED, as it always was, so a
run with no receipt gets no EXECUTED row.

Why a tag at EXECUTED rather than a branch kept until the outcome row: the
ledger names the candidate sha for as long as the ledger exists, and the
rework of a sent-back task branches from it. A branch deleted later would
orphan both unless something pinned the commit first; a tag pins it with no
bookkeeping, like `pxx-pre`. The D-3 row's "branch kept until the outcome row,
then deleted" is met in the direction it intends — nothing is lost, and nothing
is lost later either — and the divergence in letter is recorded on the WP-3
design PR.

## What the bundle says

`artifacts/branch.json`: `mode` (`branch` or `inline`), and in branch mode
`base_branch`, `base_sha`, `branch`, `candidate_tag`, `head_after`,
`asserted_by_flag`, `restored`, `parked` (the reason, when not restored). Checks:
`candidate_tagged` (ok iff a commit was pinned) and `base_branch_restored`
(ok iff step 7 completed). `asserted_by_flag` in `branch.json` records
whether the launcher passed `--task-branch` (`command.txt` is pxx's command
line, which cannot show a dx flag). The diff and status in the bundle are a
snapshot taken on
the task branch right after pxx returned, before the checkout moved.

The EXECUTED row is unchanged (`PROVENANCE_KEYS` stays `run, model, pxx,
identity`). The queue file, a convenience view, gains `branch` and
`candidate_tag`; `dx run` writes them best-effort and says so when it cannot.

## What `dx merge` does

With `--repo`, in the pre-signature checks next to the "candidate is a commit
here" check, and before any row is written:

- The target checkout is clean, and in branch mode is on `base`.
- The candidate is not already reachable from the target. If it is, the
  candidate was committed inline (before task branches): refused, and the
  refusal names `--allow-already-on-branch`. With the flag, a banner is
  printed like `--force`'s, the MERGED row records that no merge commit was
  created, and the evidence names the flag.
- The merge is conflict-free by `git merge-tree --write-tree` (git ≥ 2.38),
  which touches no file. A conflict refuses the gate with **no row written**,
  so the signature (bound to the ledger head) stays valid. The remedy stated
  is a rework admitted at the current base and seeded from the candidate's
  recorded diff, reviewed like any other. dx never rebases and never resolves
  a merge under its own name. Expected to be rare: the base moves only by
  `dx merge`, which one human runs serially.

Then SIGNED, the merge, MERGED, under `MergeLock`, as before. The merge is a
real two-parent commit and the MERGED row's `sha` is that commit.
`git_merge_no_ff` now reads git's exit status: a failed merge is aborted
(`git merge --abort`), the conflicting paths are named, and `LedgerWriteError`
is raised; only an exit-zero merge with HEAD unchanged is `AlreadyOnBranch`.
If the merge fails after SIGNED (an I/O error, or the tree changed since the
pre-check), the gate says so and exits 1 with SIGNED standing and no MERGED
row: the truthful state, corrected on the record (RL-009).

Nothing is deleted at MERGED: the task branch is already gone and the
candidate tag stays as the permanent name of what was signed.

## What `dx doctor` prints

```
🌿 task branches: on — each run on task/<task id> from main; sign-off merges for real (plan D-3)
   ✅ /home/x/ai/pilot: on main, clean, 0 stray task branch(es), 3 candidate tag(s)
```

The checkouts inspected are the executor role's declared scopes (the
manifest's `approval:` section → `roles.json`), never a path typed into
doctor. A stray task branch is listed with the two commands that return it.
With branch mode on, a checkout off its base or holding a stray branch is ❌
and fails doctor; with it off the same facts are ⚠️.

## Returning a parked checkout

```
git -C <pilot> status            # look first; nothing was reset
git -C <pilot> switch main
git -C <pilot> branch -D task/T-NNNN
```

Commit or discard the parked changes before switching. If a candidate was
tagged, it is on `candidate/T-NNNN` and survives the branch deletion. Ctrl-C
or a crash between steps 3 and 7 leaves the same shape and is resolved the
same way; there is no automatic recovery, because the tree's state at that
moment is exactly what a person needs to look at.

## Tests

`tests/test_task_branches.py`: every case against a real temporary
repository, with pxx replaced by a stub script that performs the git actions
pxx would (commit on HEAD; write and reset like pxx's restore; write without
committing; commit then exit non-zero) — never a mocked git. The legacy
inline candidate (refused, then recorded with the flag) is in
`tests/test_cmd_merge_state.py`. The conflicting `git_merge_no_ff` is a
regression test in both modes.
