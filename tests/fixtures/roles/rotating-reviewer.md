# Rotating Reviewer

**Slug:** `rotating-reviewer` · **Phase:** Review · **Agent fit:** Partial · **9-person seat:** Rotation (S3/S4/S7)

## Mandate

Reviews changes on a rotating seat. Exists to prove the seat field accepts
compound, non-`S<n>` values.

## Inputs required

- A diff

## Outputs

- A review record

## Operating checklist

1. Read the diff.

## Definition of done

- [ ] Review recorded

## Must not (separation of duties)

- **Review your own change.**

## Failure modes

- Shallow review of a large diff

## Handoff

**Receives from:** `widget-engineer`
**Hands to:** `widget-lead`

## Related

`widget-engineer`
