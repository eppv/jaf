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
        text: "toy.calc.add returns the sum of two numbers"
        check: 'python3 -c "import toy.calc as c; assert c.add(2,3)==5"'
      - id: "C-1.1.2"
        version: 1
        text: "toy.calc.mul returns the product"
        check: 'python3 -c "import toy.calc as c; assert c.mul(2,3)==6"'
      - id: "C-1.1.3"
        version: 1
        text: "the test suite is green"
        check: "make test"
    acceptance_history: []
  - id: "1.2"
    title: "powers and CLI"
    depends_on: ["1.1"]
    criteria_version: 1
    acceptance:
      - id: "C-1.2.1"
        version: 1
        text: "toy.calc.power(a, n) returns a to the power n"
        check: 'python3 -c "import toy.calc as c; assert c.power(2,10)==1024"'
      - id: "C-1.2.2"
        version: 1
        text: "python3 -m toy 2 3 --op add prints 5"
        check: "python3 -m toy 2 3 --op add | grep -qx 5"
      - id: "C-1.2.3"
        version: 1
        text: "the test suite is green"
        check: "make test"
    acceptance_history: []
---

# Development plan

Two increments: the arithmetic core first, the CLI second. Each criterion is a
command; QA is expected to run it, not to read the code.

## Increment 1.1 - calculator core

`toy/calc.py` with `add` and `mul`, plus unit tests. Standard library only.

## Increment 1.2 - powers and CLI

Depends on 1.1. Adds `power` and the `python3 -m toy` entry point.
