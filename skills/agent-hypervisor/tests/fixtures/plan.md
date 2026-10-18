---
plan_ver: 3
increments:
  - id: "1.1"
    title: "first"
    depends_on: []
    criteria_version: 1
    acceptance:
      - id: "C-1.1.1"
        version: 1
        text: "add works"
        check: 'python3 -c "import toy.calc as c; assert c.add(2,3)==5"'
      - id: "C-1.1.2"
        version: 1
        text: "tests green"
        check: "make test"
    acceptance_history: []
  - id: "1.2"
    title: "second"
    depends_on: ["1.1"]
    criteria_version: 1
    acceptance:
      - id: "C-1.2.1"
        version: 1
        text: "power works"
        check: "make test"
    acceptance_history: []
---

# Plan

## Increment 1.1 — first

Body of the first increment.

## Increment 1.2 — second

Body of the second increment.
