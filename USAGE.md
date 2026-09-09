# Using dx: spec-driven coding

The promise of this tool is consistency. You write a **spec** — a short, structured
description of one coding task — and `dx` turns it into code, a tamper-evident
receipt, and (when you choose) a governed merge. The same spec, run again, gives
you the same shape of result, because the only thing that varies is the spec: `dx`
holds the role, the routing, and the gates constant.

If you take one thing from this page: **keep your specs as files and hand them to
`dx run`.** A file you can reuse and refine beats a sentence typed at a prompt.

## The loop

1. **Write the spec.** Copy [`templates/spec.md`](templates/spec.md), rename it
   (say `specs/T-042-add-csv-export.md`), and fill every field. A filled example
   is in [`templates/spec.example.md`](templates/spec.example.md).

2. **Run it.**

   ```bash
   dx run --required_role backend-engineer --scope . \
          --message "$(cat specs/T-042-add-csv-export.md)"
   ```

   `dx` wraps your spec after the role's mandate and separation-of-duties rules,
   routes it to your configured hardware, and invokes the code generator. It
   writes a `dx.role_task.v1` evidence bundle — what ran, what changed, and a
   `boundary` block stating what the bundle does *not* prove.

3. **Check the result.** Read the code, then run the tests your spec named. The
   evidence bundle is under `~/.local/state/dx/evidence/<task>/…`; verify it with
   `sha256sum -c SHA256SUMS`. `dx` does not claim the code is correct — that is
   your acceptance step, which is exactly why the spec lists acceptance criteria.

That is the whole loop. Everything below is detail.

## Reading the result — check acceptance, not the exit code

`dx run` reports the code generator's own exit code (`3` means the tool exited
non-zero). A non-zero exit does **not** always mean the code is wrong: some models
try to run a shell command mid-task, and the generator fails closed when no shell
safeguard is configured — after the file was already written correctly. When that
happens, dx tells you: instead of a flat "task failed" it prints **"pxx exited N,
but the scope changed — a non-zero exit is not proof the work is wrong"** and
points at the receipt. The exit code stays `3` (the tool did not cleanly finish),
because the judgement is yours: read the code and run the tests your spec named
before you trust or discard the result. This is why your spec lists **acceptance
criteria** and why every run leaves an evidence bundle whose `produced_changes`
check records that files were written even when the exit code was non-zero.

If you want a clean exit when a model reaches for the shell, configure the
generator's shell handling once (a `PreToolUse` hook, a sandbox, or an explicit
`PXX_ALLOW_UNGATED_SHELL=1`) — see pxx's `docs/CONFIG.md § hooks`. It changes the
exit code, not the code.

## Why a structured spec gives a consistent result

Each role card already declares the **inputs it expects**: an objective, non-goals,
a base commit, allowed and prohibited paths, dependencies, required tests, and a
named reviewer. The template is those inputs as fill-in-the-blank fields. When you
supply them, the model is working from the same frame every time; when you leave
them out, it fills the gaps differently on each run. The structure is the
consistency.

The two fields that most change the outcome:

- **Non-goals** — what *not* to do. This stops the model from adding a package, a
  config file, or a second feature you did not ask for.
- **Allowed and prohibited paths** — where it may write. `--scope` bounds this at
  the tool level; naming the paths in the spec bounds it in the model's head too.

## Pick a role

The role sets the mandate and the routing. For a coding task, pick by what the work
*is*:

| The task is… | Role |
| --- | --- |
| Server logic, APIs, persistence | `backend-engineer` |
| UI, screens, client-side code | `frontend-engineer` |
| Mobile app code | `mobile-engineer` |
| Pipelines, ETL, data plumbing | `data-engineer` |
| Making existing code faster | `performance-engineer` |
| CI, build, deploy, infra-as-code | `platform-engineer` |
| Tests for existing code | `sdet` |

`dx roles list --fit High` shows the roles best suited to autonomous runs. Seven
roles are **Anchored** (`dx roles list --anchored`): they require a named
accountable human and `dx run` refuses to execute them autonomously. That refusal
is deliberate, not a bug.

## Verify and merge

- **Verify a GUI**, if the task produced one: `dx verify-gui` captures the screen
  and checks it with a vision model, writing a `dx.gui_verification.v1` bundle.
- **Merge under governance**: `dx merge <task>` runs the RL-003 signature gate and,
  with `--repo`, performs the `git merge --no-ff` and records it. It writes a
  `dx.merge_gate.v1` bundle. The reviewer who signs must not be the code's author —
  that is the separation of duties the whole tool is built around.

`TUTORIAL.md` walks the full path from a clean box to a merged task.

## One habit that pays off

Keep a `specs/` directory of the specs you have run. They are the most reusable
artifact you produce: a spec that gave a good result once is a template for the
next similar task, and refining a spec is how you get a *better* result rather
than a different one.
