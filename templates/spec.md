# <TASK-ID> — <one-line title>

<!--
HOW TO USE THIS FILE
  1. Copy it, rename it (e.g. specs/T-042-add-csv-export.md), and fill every field.
  2. Run it:  dx run --required_role <role> --scope <path> --message "$(cat this-file.md)"
  3. dx wraps this whole file as the USER INSTRUCTION, after the role's MANDATE and
     MUST NOT. The mandate is the constant; this spec is the variable — so the more
     precisely you bound the work, the more consistent the result.
Delete these comments once filled, or leave them; dx ignores them.
See USAGE.md for the full loop and how to pick a role.
-->

- **Role:** `backend-engineer`   <!-- who does it — see USAGE.md § Pick a role -->
- **Scope:** `.`                 <!-- the directory dx may edit; nothing outside it is touched -->
- **Base commit:** `<sha or "current HEAD">`

## 1. Objective

<!-- What to build and why, in 2–4 concrete sentences. Testable, not aspirational.
     Good: "Add a `to_csv(rows)` function in report.py that writes RFC-4180 CSV to a
     path, and a --csv flag on the CLI that calls it." Bad: "Improve exporting." -->

## 2. Non-goals

<!-- What NOT to do. The most valuable section — it stops scope creep and keeps
     results consistent run to run. List things a reasonable model might otherwise add. -->

## 3. Allowed and prohibited paths

- **Allowed:** <the files or directories this change may touch>
- **Prohibited:** everything else. If the work turns out to need a prohibited path,
  stop and revise this spec rather than widening it silently.

## 4. Constraints and invariants

<!-- The rules the result must respect. Examples:
     - Standard library only (or: may use `requests`, nothing else).
     - Follow the existing error-handling pattern in <file>.
     - Public function signatures in <file> must not change.
     - Fail closed on bad input. -->

## 5. Acceptance criteria and required tests

<!-- How you (and the evidence bundle) will know it is done. Be specific:
     inputs → expected outputs, and the tests that should exist and pass.
     Examples:
     - `to_csv([{"a":1}])` returns "a\r\n1\r\n".
     - A test that a field containing a comma is quoted.
     - `pytest` and `ruff check .` pass. -->

## 6. Reviewer

<!-- The named human who signs off. dx merge enforces reviewer != author, so this
     is who runs `dx merge` with their GPG key — not the person who wrote the code. -->
