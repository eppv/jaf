---
plan_ver: 1
increments:
  - id: "1.1"
    title: "calculator core"
    depends_on: []
    criteria_version: 1
    acceptance:
      - id: "C-1.1.1"
        version: 1
        text: "toy.calc.add(a, b) returns the sum"
        check: "python3 -c \"import toy.calc as c; assert c.add(2,3)==5\""
      - id: "C-1.1.2"
        version: 1
        text: "make test is green"
        check: "make test"
    acceptance_history: []
---

# Development plan

One section per increment: context, constraints, what "done" means. Criteria
live in the front-matter — the dispatcher reads them there and QA is judged
against them, so they are data, not prose.

## Increment 1.1 — calculator core

Module `toy/calc.py` with basic operations and tests. No I/O in the library:
the CLI appears in the next increment.

Constraints: standard library only.

## Increment 1.2 — CLI and powers

Depends on 1.1. Adds `power` and the `python3 -m toy` entry point.
