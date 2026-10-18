---
plan_sha: "0000000000000000000000000000000000000000000000000000000000000000"
criteria_version: 1
role: qa
cycle: 1
attempt: 1
---

# QA verdict

## Verdicts

| criterion | verdict | evidence |
|---|---|---|
| C-1.1.1 | pass | `python3 -c "…"` → exit 0 |
| C-1.1.2 | fail | `make test` → 1 failed (see defects) |

Every criterion id from the plan appears here exactly once, with `pass` or
`fail`.

## Defects

For every `fail`: the command, the input, the observed result, the expected
result.

## Debts

Accepted, non-blocking-for-this-increment debt.
