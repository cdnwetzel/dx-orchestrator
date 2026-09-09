# Recommendation to pxx: an honest green for edits that outlive a refused shell

**Audience:** pxx maintainers (`cdnwetzel/pxx`, the `v2` / 2.5.4 line).
**Written from:** dx-orchestrator, which runs `pxx edit` as its code-generation
engine. Grounded in the 2.5.4 source (`origin/v2`), confirmed line-for-line
against the installed `pxx-orchestrator==2.5.4`.
**Status:** proposal. dx 0.16.0 already addresses the *symptom* on its side; this
is the upstream fix that would remove the need.

## The problem, concretely

Run `dx run` (which shells out to `pxx edit`) against a task whose model reaches
for a shell command, on a box with no shell safeguard configured. pxx writes the
requested file correctly, then — because a `run_shell` in EDIT mode is
fail-closed without a safeguard — aborts and exits **2**. dx maps any non-zero
pxx exit to its own "task failed" (exit 3). The result:

```
[HOOKS_MISSING] run_shell in permission mode 'edit' requires a shell safeguard
  … — 1 file already modified: temp_convert.py (rounds=0 …)
❌ pxx task failed (pxx exit 2).
```

The generated code met every acceptance criterion. The exit code said throw it
away. This is the "non-zero but green" case.

## Root cause: pxx knows the truth, then discards it at the process boundary

pxx already tracks whether files changed, and it records the truth even on the
abort path — but the value it *returns* (and exits on) is a different, zeroed
copy.

1. **The abort-path outcome is zeroed.** `pxx/session.py:275-277`:

   ```python
   except HooksMissing as exc:
       outcome = RunOutcome(
           code=TerminalCode.HOOKS_MISSING, summary=str(exc), session_id=self.session_id
       )
   ```

   No `files_changed=` is passed, so it defaults to `0` — even though a file was
   already written. The same is true of the other early-exit handlers
   (`OUT_OF_SCOPE`, `CONFIGURATION_INVALID`, …).

2. **The audit record, by contrast, is truthful.** A few lines down
   (`session.py:612`, `:628`) the terminal record is written from a *projection*
   of the event history:

   ```python
   projected = project_outcome(self.bus.history, self.session_id)
   writer.write_outcome({ …, "files_changed": projected.files_changed, … })
   ```

   So the JSONL audit log carries the real `files_changed` on every path,
   including HOOKS_MISSING.

3. **The exit code consults neither.** `pxx/cli.py:108`:

   ```python
   def exit_code_for(outcome: RunOutcome) -> int:
       if outcome.code is TerminalCode.COMPLETED:
           return 0
       if outcome.code in _GATE_CODES:      # HOOKS_MISSING ∈ _GATE_CODES (cli.py:82)
           return 2
       if outcome.code is TerminalCode.INTERRUPTED:
           return 130
       return 1
   ```

   It maps by terminal code alone. `files_changed` never enters the decision, and
   the `outcome` it receives is the zeroed one from (1), not the projected one
   from (2).

So the "a correct edit was produced" signal exists inside pxx and is written to
its own record, but is thrown away at the exit-code boundary — the one surface a
caller like dx actually reads.

## Recommendations, smallest first

### 1. Make the returned outcome carry the true `files_changed` on every path

Attach the projected `files_changed` to the early-exit `RunOutcome`s in
`session.py` (HOOKS_MISSING, OUT_OF_SCOPE, CONFIGURATION_INVALID, …), so the
object pxx *returns* matches the record it *writes*. This is a correctness fix on
its own: today `write_outcome` and the returned outcome disagree about the same
run. Everything below depends on it.

### 2. Split the exit-code contract; refine it by whether an edit was produced

"Could not run under the configured policy" is not "the work failed." `outcome.py`
already groups these — its comment calls HOOKS_MISSING / MODEL_UNAVAILABLE /
CONFIGURATION_INVALID the "boundary / config / model" class. Give that class its
own exit code, and let `files_changed` (now truthful, per #1) split "wrote an
unverified edit" from "did nothing":

```python
_SETUP_CODES = {
    TerminalCode.HOOKS_MISSING,
    TerminalCode.MODEL_UNAVAILABLE,
    TerminalCode.CONFIGURATION_INVALID,
}

def exit_code_for(outcome: RunOutcome) -> int:
    if outcome.code is TerminalCode.COMPLETED:
        return 0
    if outcome.code in _SETUP_CODES:
        # Setup/policy prevented the run from finishing. Distinguish "produced an
        # unverified edit" from "produced nothing", so a caller is not told the
        # work failed when code was written.
        return 3 if outcome.files_changed else 2
    if outcome.code in _GATE_CODES:   # a real gate stopped a run that DID run
        return 2
    if outcome.code is TerminalCode.INTERRUPTED:
        return 130
    return 1
```

The exact numbers are a design choice; the point is that a caller can now tell
`0` (clean) from `3` (setup blocked it, but an edit exists) from `2` (blocked,
nothing produced) from `1` (ran and failed). This mirrors the 2-vs-3 split dx
already draws for its own governance codes, so dx could map pxx's result directly
instead of reconstructing intent from `git`.

Recommendations 1 and 2 are safe: they preserve the fail-closed-loud posture and
only make an existing, internally-recorded signal honest at the boundary.

### 3. The only true exit 0 — graceful degrade, opt-in

The abort exists because a single `run_shell` (mapped to the SHELL action class
in `broker.py`) raises `HooksMissing`, which ends the whole session. An
alternative: **deny that one action** — hand the model a failed tool call it can
react to — and let the edit loop finish on its own terms (`COMPLETED` → exit 0).
The run then genuinely succeeds, because the only thing blocked was an optional
shell, not the code.

This changes a safety default (from "abort the run" to "deny the shell, keep
editing"), so it must be opt-in:

```
on_missing_shell_safeguard: "abort" | "deny"   # default "abort" — today's behaviour
```

It is the only option that produces a real green rather than a legible non-zero,
and the only one that needs a policy decision before shipping.

## Where the change goes, and a version caveat

- The target is **`origin/v2` = 2.5.4**, which matches the `pxx-orchestrator`
  wheel dx installs. `#1` and `#2` are a few lines in `session.py` and `cli.py`.
- The local `~/ai/pxx` **`main` is 1.3.4** — a different, loop-based architecture
  (`loop.py`, `outcomes.py`, `_terminal`). Do not port these against `main`; the
  code shape does not match.

## Recommendation

Ship **1 + 2** together as the honest-green fix. They are small, contained, and
preserve pxx's security stance — they just stop pxx from discarding, at the exit
code, a fact it already writes to its own audit record. Keep **3** as a
follow-up if you want autonomous runs to exit clean, behind the opt-in flag.

On the dx side, 0.16.0 already reframes a non-zero-with-changes run as an
advisory rather than a flat failure and keeps its own exit code at 3 on purpose
(the exit code is the tool's; the judgement is the operator's). If pxx adopts
`#2`, dx can key off pxx's exit code directly instead of re-deriving "did it
write code" from `git status`.
