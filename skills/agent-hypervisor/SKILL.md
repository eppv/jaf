---
name: agent-hypervisor
description: Drive sequential agent roles from a frozen, hashed plan.
version: 0.1.0
author: Eugene Popov (eppv), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Agents, Orchestration, Git, Workflow]
    related_skills: []
---

# Agent Hypervisor Skill

One dispatcher, several short-lived role sessions, files as the only memory.
The plan is a frozen contract with executable acceptance criteria; sessions are
spawned one at a time as child processes; every transition is a rule in the
dispatcher, not an opinion in a chat. Used to run a software project where the
code is written by agents and reviewed by the owner at cycle boundaries.

What it is not: a parallel agent swarm, a merge-conflict resolver, or a
replacement for your own judgement about the plan.

## Installation

This skill is more than `SKILL.md`: it ships a dispatcher (`scripts/`), prompt and
artifact templates, a toy project and tests. Install the **whole directory**.

- **From the repository (the only supported way).** Clone and copy, or symlink:

```
git clone --branch <branch> https://github.com/eppv/jaf.git /tmp/jaf
cp -r /tmp/jaf/skills/agent-hypervisor <skills-dir>/<category>/
```

- **From a raw `SKILL.md` URL — does not work here.** `hermes skills install
  <raw url>` stores only the files it happens to scan; the dispatcher then fails
  because `scripts/hvlib/` and `templates/` are missing. If you installed that
  way, remove it and install from the tree.

Updates: re-clone and copy over. The repository tree is the single source of
truth — do not edit the installed copy, or the two drift silently.

## When to Use

- The owner asks to develop a project with agent subcontractors and a
  supervising session ("agent-hypervisor", "hypervisor approach", "supervisor
  agent", "subcontractors").
- An existing `hv.toml` project needs to be advanced: `hv status`, `hv run`,
  review the cycle report, answer `AWAITING_HUMAN`.
- A role session must be inspected: prompts in `run/prompts/`, output in
  `run/logs/`, artifacts in `docs/reports/`.

Don't use for: a single-file edit, a quick question about code, anything where
the owner wants to talk to one agent rather than run a process.

## Prerequisites

- `git` with worktree support, `python3` ≥ 3.11 (stdlib only — no PyYAML), `sh`,
  `tar`, `make` for the gates.
- An executor CLI for the roles. There is **no default**: `hv init` requires
  `--adapter-cmd`. Proven lines are in `references/protocol.md` §12 (Hermes,
  Qwen, Claude Code, Codex, and the deterministic fake `/examples/adapters/fake.py`).
- Optional: a reachable docker daemon for `isolation.mode = "docker"`. Without
  it `hv` degrades to `isolation=none` with a warning and says so in STATE.
- The project's `make` targets must be defined by the enabling-engineer; gates
  with an empty command are simply skipped.

## How to Run

The dispatcher is a script in this skill. Call it through the `terminal` tool
from the project root (a nearest-`hv.toml` lookup also works):

```
python3 <skill_dir>/scripts/hv.py status
python3 <skill_dir>/scripts/hv.py run --until blocked
```

`<skill_dir>/bin/hv` is the same thing as a wrapper. First time in a project:

```
python3 <skill_dir>/scripts/hv.py init --name myproj \
  --adapter-cmd 'hermes -z "$(cat {prompt_file})" --usage-file {usage_file}' \
  --isolation docker
```

`hv init` creates `hv.toml`, the orphan `spec` branch (`PLAN.md`,
`DECISIONS.md`, `env/ENV.md`, `roles/`), `run/`, `worktrees/`, the `.hv/spec`
checkout, and git excludes — it does not commit the owner's work.

## Quick Reference

```
hv status [--json]           where we are, what is blocked, what waits
hv plan                      increments, criteria versions, pins
hv run [--until blocked|cycle|accept|done] [--max-cycles N]
hv report [cycle]            fixed-shape cycle report
hv gate [--cold]             deterministic gates
hv spawn <architect|enabling|dev|qa>
hv decide --kind ... --text "..."     append a decision to DECISIONS.md
hv resolve --decision D-NNN [--stage S]
hv approve|reject|reopen --decision D-NNN ...
hv rebuild                   re-derive STATE from git
hv log [--tail N]
```

Exit codes: `0` ok, `2` blocked or refused (reason printed).

## Procedure

1. **Orient.** Run `hv status`. If the stage is `AWAITING_HUMAN`, read `reason`
   and `options`; if `BLOCKED`, read the cause. Completion: you can state the
   stage, the increment, `plan_sha` and what is waiting, without opening files.
2. **Human decisions come first, in writing.** The owner's answer becomes
   `hv decide --kind ... --text "..."`, and only then `hv resolve` / `approve` /
   `reject` / `reopen` with `--decision D-NNN`. Refusing without a decision is
   the intended behaviour — do not work around it by editing STATE.
3. **Drive.** `hv run --until blocked` and read the printed status. It returns
   only at a closed cycle, an attempt limit, a gate failure, or a question.
   Completion: the stage is one of `AWAITING_HUMAN`, `BLOCKED`, `DONE`, or a
   cycle just closed and `hv report` renders.
4. **Report to the owner** with `hv report` plus smells (`hv status` lists
   them): criteria churn, repeated rejections, repeated QA failures on the same
   criterion, cost per role. Batch questions to boundaries; do not micro-manage.
5. **Never** edit `run/STATE.json`, `run/log.jsonl`, the spec branch or the
   project code by hand; never spawn a role to "just fix" something.

## Layout

```
skills/agent-hypervisor/
  SKILL.md                     this file: the role and the loop
  scripts/hv.py                CLI; dispatcher is the only writer of STATE
  scripts/hvlib/*.py           miniyaml, plan, state, gitops, gates, adapter,
                               prompts, dispatch, report, decisions
  templates/roles/*.md         role contracts rendered into session prompts
  templates/prompts/*.md       assignment fragments (normal, planning, rules)
  templates/artifacts/*.md     canonical PLAN / STAGE / QA / DECISIONS shapes
  templates/project/hv.toml    project config written by `hv init`
  templates/spec/*             skeleton of the spec branch (PLAN, DECISIONS, env, roles)
  references/protocol.md       the full contract: storage, transitions, policies
  bin/hv                       wrapper for scripts/hv.py
  examples/toy/                the toy project each role plays against
  examples/adapters/fake.py    deterministic executor for tests and demos
  tests/                       unit + end-to-end tests (unittest, no deps)
  tests/fixtures/              plan fixture shared by the unit tests
```

Every piece of prose — role contracts, prompt fragments, project and spec
skeletons, the toy project, report templates — lives in `templates/` or
`examples/` as a file. Python modules only assemble and validate it; if you
find yourself adding a multi-line string constant to a module, put it in a
template instead.

Project side: `hv.toml`, `run/`, `worktrees/cycle-NN/`, `.hv/spec/`, `.spec/`
inside each worktree (read-only copy of the spec branch).

## Pitfalls

1. **`plan_sha` is a content hash, not a commit.** Editing `roles/*.md` or
   `env/` must not invalidate artifacts; if it does, the hash is computed over
   the wrong object.
2. **The plan is frozen mid-cycle.** Touching `PLAN.md` without bumping
   `criteria_version` stops the cycle with "frozen plan violated" — that is the
   guard against silently weakening a criterion, not a bug.
3. **Counters are not retries.** A timeout/crash never consumes `rejections`;
   the `total_rejections` fuse is deliberately not reset by a plan revision, so
   a project cannot be walked around the limit forever.
4. **`main` may only be merged by the dispatcher**, and only from a clean
   checkout: a dirty root is `BLOCKED`, not a merge conflict.
5. **`make test` with zero tests can exit non-zero** on recent Pythons ("NO
   TESTS RAN", exit 5). The enabling-engineer's Makefile must handle an empty
   `tests/` explicitly.
6. **Docker socket unavailable is not an error.** It degrades to
   `isolation=none`; the cold-container gate reports `skip`. Say so in the
   report instead of treating the run as sandboxed.
7. **Report artifacts are validated mechanically.** A missing section or a
   missing verdict is a rejected artifact, retried once with the defect list
   written next to it (`RETRY-*.md`), then escalated.
8. **`hv rebuild` is the only way back** after a lost `run/`: it re-derives
   STATE from tags, branches and artifacts.

## Verification

- `hv status` answers where/who/what-waits after a fresh process (`/new`).
- `hv run --until cycle` on the toy project in `tests/` closes a cycle: tag
  `cycle-NN-accepted` exists, `main` has code and reports but no `PLAN.md` or
  `env/`, and `hv gate` in the project root is green.
- `python3 -m unittest discover -s tests` in this skill passes (39 tests:
  plan hashing, schema validation, gate parsing, container mounts, full cycles,
  crash/timeout/fuse paths, decision flow, bundle transfer, spec skeleton).

## References

- `references/protocol.md` — canonical spec: storage rules, artifact schemas,
  transitions, counters, merge policy, adapter contract, isolation, transfer.
- `templates/artifacts/*.md` — copy these shapes when hand-writing an artifact.
- `examples/adapters/fake.py` — the reference executor; its `FAKE_MODE`
  switches reproduce every failure path deterministically.
