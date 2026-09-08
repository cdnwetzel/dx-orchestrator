# Widget Engineer

**Slug:** `widget-engineer` · **Phase:** Build · **Agent fit:** High · **9-person seat:** S4

## Mandate

Builds widgets and owns their correctness. Synthetic fixture content — this card
exists only to exercise the parser.

## Inputs required

- A widget specification
- An assigned branch

## Outputs

- Working widget code
- Tests that fail before the change

## Operating checklist

1. Reproduce the behavior first.
2. Make the smallest change.

## Definition of done

- [ ] Tests added
- [ ] Diff confined to allowed paths

## Must not (separation of duties)

- **Review or approve your own widget.** Someone else records the approval.
- **Override a failing deterministic gate.** Fix the code or change the gate on the record.

## Failure modes

- Silent scope expansion
- Prose evidence in place of command output

## Handoff

**Receives from:** `widget-lead`
**Hands to:** `widget-reviewer`

## Related

`widget-reviewer`, `widget-lead`
