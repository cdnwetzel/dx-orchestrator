# The reviewer leg: what dx asks pxx for, and what the bundle says about it

**Status:** implemented (WP-4 of the PS Coding program); advisory only, by
decision D-4 (2026-10-02). **Design:** AskPS `docs/general/ps-coding-wp4-design.md`
(Kimi design review 2026-10-05, PASS-with-amendments).

## The problem it closes

Every `dx run` bundle to 2026-10-02 showed `review_seconds: 0`. Not because
the reviewer was slow: it never ran. pxx's loop runs its reviewer (Guard 4)
only when `--review` is passed or `loop_review` is set, and dx passed neither.
A reviewer that is configured, named in a decision, and never invoked is the
worst of the three states, because the record looks the same as a reviewer
that ran and had nothing to say.

## The choice lives in the manifest

```yaml
# ~/.config/dx/hardware_manifest.yml  (and config/manifest.template.yml)
review:
  enabled: true
  mode: advisory
```

`dx.config_loader.get_review_config()` reads it the way `get_approval_config`
reads its section: absent → `None`; present but malformed → `ConfigError`, so
a typo cannot become "review off" silently. `validate_manifest` calls it, so
`dx doctor` fails on a bad section before the first run does.

`mode: blocking` is refused by the loader with a message naming D-4. pxx
supports the value; dx does not offer it until a calibration run has measured
what the reviewer's findings cost in rounds. `REVIEW_MODES` in
`config_loader.py` is the one place that gate is, and lifting it is a
one-line, reviewed change.

The generator (`scripts/gen_manifest.py`) carries `review:` from the
**template**, not the binding: the section is address-free and program-wide,
and one tracked file decides it.

## The command: a flag on every run

```
pxx loop --scope … --message … --sandbox --review --review-mode advisory [--commit]
pxx loop --scope … --message … --sandbox --no-review [--commit]
```

Both flags together, or `--no-review` alone, never a mix. dx states the choice
on every run so pxx's own `loop_review` default — and a `PXX_LOOP_REVIEW` in
some unit — never decides behind the manifest's back (the explicit flag wins in
pxx). `--dry-run` prints the flags it would send and, when off, why.

## The record: `result.review`, from the run record only

`dx.run_facts.review_fact` builds the fact the way the tests fact is built:
from pxx's `events.jsonl` (`gate_decision` events with `gate: "review"` and
`"review_stale"`) and `outcome.json`. Nothing the model said is consulted.
Three shapes, each explicit:

```json
{"requested": true, "mode": "advisory", "ran": true, "runs": 2,
 "verdict": "APPROVE", "findings": 0, "allowed": true, "stale_rereviews": 0,
 "review_seconds": 41.2, "unparseable_review_count": 0,
 "contributing": ["REVIEW_REJECTED"], "run_id": "20261005T…-loop-…"}

{"requested": true, "mode": "advisory", "ran": false,
 "why": "no review gate in the run record: the loop ended ROUND_CAP before its review gate (review runs only after a round's tests pass)",
 "run_id": "…"}

{"requested": false, "why": "no review section"}
```

"Requested but did not run" is never inferred from `review_seconds == 0`:
zero is also what a review that failed instantly reports. The review gate
events are copied verbatim to `artifacts/pxx-review-gates.jsonl`, one line
per review, so a reader sees every round's verdict, not only the last.

The bundle check `review_recorded` is ok when the reviewer was not requested
or ran, and false when it was requested and did not run, with `why` as its
detail. It is a recorded fact, not a gate on `result.passed`: `passed` is
pxx's exit, and a loop that never reached its review gate already exited
non-zero. A second signal for the same fact would make an advisory reviewer a
gate by the back door, which D-4 forbids.

The README's one line comes from `dx.evidence.render_review_line`, which the
AskPS bridge reuses word for word, so the two surfaces cannot disagree on what
"did not run" means.

## What the record cannot say, and what dx says instead

pxx's gate event does not name the reviewer model: `ReviewPacket.reviewer` is
the literal string `"reviewer"`. So `artifacts/routing.json` carries
`reviewer_configured`: the `effective_review_model` pxx's own `load_settings`
resolves for this scope under the run's environment, read at launch through
`dx.cmd_run._reviewer_route`. It is labelled for what it is — the reviewer
configured at launch, not a fact from the run — and it is `null` when pxx is
not importable where dx runs. The manifest does not gain a reviewer lane:
declaring the reviewer twice is a second place to drift. The right fix, a pxx
event that names the reviewer, is proposed upstream separately.

## `dx doctor`

Two derived lines, never restated:

```
🧪 reviewer leg: on, advisory (manifest review:)
   ✅ reviewer devstral:24b @ http://…/pt/mac (ollama) differs from writer Nemotron-3.5 @ … (backend-engineer, …)
```

or `off (manifest has no review section) — F-001: the reviewer never runs`.
The comparison is printed per distinct writer route, under that route's
environment, even with the reviewer off, so a box can be checked before the
switch. A reviewer that cannot be determined is `⚠️`, never `✅`; the same
model as the writer is `❌ decorrelation broken`. Non-core: doctor's exit does
not change.

## The proving run (human, on the execution host)

1. Add the `review:` section to the box's manifest (advisory).
2. `dx doctor --no-network` — paste back the posture lines and the resolved
   timeout.
3. One small task from a PS Coding chat. Acceptance (program F-001): the
   bundle's `result.review` shows `ran: true`, `review_seconds > 0` and a
   verdict; the bridge's review surface shows the reviewer line.
4. The calibration run for D-4 is not this package.

## Risks, stated

A `REVISE` verdict starts a healing round in **both** modes (pxx
`loop.py`, the REVISE branch precedes the `blocked` check), so findings cost
rounds even in advisory mode; a strict reviewer on a correct diff can push a
run to `LOOP_DETECTED` or the round cap. The calibration run measures that;
this package only records it, and the manifest switch turns the reviewer off
without a code change. A diff over the reviewer's context is `NO_REVIEW` →
recorded as `REVIEW_UNAVAILABLE`, never a silent pass.
