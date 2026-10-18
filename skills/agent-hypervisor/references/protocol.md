# agent-hypervisor protocol v1

The contract between the dispatcher (`scripts/hv.py`), the project it drives
and the role sessions it spawns. This file is the canonical spec: if the
implementation and this document disagree, one of them is a defect.

## 1. What the system is

One dispatcher, several short-lived role sessions, no shared memory. The
dispatcher applies rules and is the only writer of state; roles produce
artifacts and nothing else.

Three consequences everything else follows from:

1. **Sessions are sequential, not parallel.** A parallel executor breaks on
   implicit decisions: the action carries decisions that appear neither in the
   report nor in the prompt, so two executors make incompatible choices and the
   result cannot be merged. One writer removes this failure class by
   construction; extra "intelligence" is attached on the read side (review,
   acceptance).
2. **A session never carries context forward.** Every attempt starts fresh from
   a rendered prompt: role contract + the increment's plan block + the previous
   artifact. Memory lives in files.
3. **Everything that a shell command can decide is decided by a shell
   command.** Tests, build, lint, schema checks and merges are gates, not
   opinions. Roles judge only what cannot be executed.

Scaling therefore comes from the quality of the decomposition, not from the
number of parallel workers.

## 2. Storage: three kinds of state, one writer each

| Storage | Contents | Owner | When it changes |
|---|---|---|---|
| `spec` branch (git) | `PLAN.md`, `DECISIONS.md`, `roles/*.md`, `env/` | per file, see below | `PLAN.md`, `roles/*` — at a cycle boundary; `DECISIONS.md` — append-only at the moment of a decision; `env/` — any time |
| code worktree per cycle | implementation + `docs/reports/*` | developer, QA, dispatcher | inside the cycle, each role in its own files |
| `run/STATE.json`, `run/log.jsonl`, `run/cost/*.json` | current state, append-only log, cost per session | dispatcher | nobody else, ever |

`PLAN.md` and `roles/*.md` belong to the architect and change only on a cycle
boundary. `env/` belongs to the enabling-engineer and is the **single exception**
to that rule: a broken environment cannot wait for a boundary. The price of the
exception is explicit — any change to `env/` invalidates the previous gate run,
and every artifact pins `env_sha`.

`spec` is never merged into `main` and never handed to a session as a git
object. The dispatcher materialises it as a plain read-only copy at
`<worktree>/.spec`; during ENV, PLANNING and ACCEPT the copy is writable for
the owning role and is copied back to the branch afterwards.

The code branch must never contain `PLAN.md`, `DECISIONS.md` or `env/`. If the
contract leaks into code history, the two streams start to drift.

## 3. Roles

| Role | Produces | Ownership |
|---|---|---|
| architect | `PLAN.md` (increments + executable criteria), `DECISIONS.md` entries, `roles/*.md` | the plan, nothing else |
| enabling-engineer | `env/ENV.md` on `spec`; `Makefile`, `Dockerfile`, toolchain pins on the code branch | the environment |
| developer | code, tests, `docs/reports/STAGE-CC-attempt-K.md` | implementation + its report |
| QA | `docs/reports/QA-CC-attempt-K.md` | verdicts, never the code |
| hypervisor (human-facing agent) | the cycle report, questions to the human | no artifacts at all |

Roles are called conditionally: enabling on the first cycle and when the
environment breaks; architect at boundaries and when a plan revision is
required; QA after every developer attempt.

## 4. Cycle = increment

A cycle is one increment of the plan, not the whole project.

```
cycle N (plan frozen)
  architect   → PLAN §increment N + acceptance criteria (executable)
  enabling    → env/ (only when triggered)
  dev         → code + STAGE-NN-attempt-K.md
  qa          → QA-NN-attempt-K.md (pass/fail per criterion)
  architect   → DECISIONS.md entries, increment marked accepted_in: N
  ───
  dispatcher  → squash, merge, regression gate, tag, next cycle
```

The plan is frozen for the duration of the cycle. The only plans edits allowed
mid-cycle are versioned criteria revisions, which reopen the cycle (§7).

## 5. Artifacts and pins

Every role artifact carries `plan_sha` and `criteria_version`; without them it
is impossible to answer "did QA check what dev built?".

**`plan_sha` is not a commit hash.** It is sha256 over a normalised block:
the increment's front-matter entry (without `plan_sha` and `accepted_in`) plus
its body section, with trailing whitespace stripped and blank-line runs
collapsed. Editing `roles/*.md` or `env/` therefore does not invalidate
artifacts, while editing the increment does.

Two more independent pins:

* `criteria_version` — the version of this increment's `acceptance[]`;
* `env_sha` — the git tree hash of `env/` on the spec branch.

Three hashes answer three different questions. Any mismatch blocks acceptance
and names what diverged.

### PLAN.md shape

Front-matter carries the machine-readable contract; the body carries context.

```yaml
plan_ver: 4
increments:
  - id: "2.1"
    title: "data access layer"
    depends_on: ["1.2"]
    criteria_version: 3
    acceptance:
      - id: "C-2.1.1"
        version: 3
        text: "repositories return domain objects, not rows"
        check: "make test -k repositories"
    acceptance_history:
      - id: "C-2.1.1"
        version: 2
        text: "previous formulation"
        check: "make test -k repositories"
        reason: "narrowed to the repository boundary"
```

Rules enforced by `hv`:

* ids are unique; `depends_on` references existing increments and is acyclic;
* every criterion has a `check` — an executable command, not prose;
* a criterion with `version > 1` must have a lower version in
  `acceptance_history` (old versions are never deleted);
* `accepted_in` is set by the architect at acceptance, never by the dispatcher.

### Report schema (minimum)

`STAGE-CC-attempt-K.md` — front-matter `plan_sha`, `criteria_version`, `role`,
`cycle`, `attempt`; body sections `What was done`, `Deviations and implicit
decisions`, `Open questions`, `How to verify`.

`QA-CC-attempt-K.md` — same front-matter; body sections `Verdicts` (one row per
criterion id, `pass|fail`, with evidence), `Defects` (reproduction for each
fail), `Debts`.

A missing section is not a style problem: the artifact is rejected, the session
is restarted once with the defect list, and after a second failure a human is
asked.

## 6. Transitions

Deterministic and implemented in the dispatcher; judgement belongs to roles.

```
IDLE      → PLANNING                        (new cycle, branch cycle/NN off main)
PLANNING  → ENV | DEV                       (env missing → ENV; otherwise DEV)
ENV       → DEV                             (cold setup + build + test green)
DEV       → QA | DEV | PLANNING | AWAITING_HUMAN | BLOCKED
QA        → ACCEPT | DEV | PLANNING | AWAITING_HUMAN
ACCEPT    → IDLE | BLOCKED                  (squash, merge, regression gate, tag)
IDLE      → DONE                            (no increment left)
```

Counters distinguish quality failures from infrastructure failures:

| Counter | Meaning | Default limit | On limit |
|---|---|---|---|
| `rejections.dev` | dev artifact rejected (schema, gate, verdict) | 2 | architect revises the plan (PLANNING) |
| `rejections.qa` | QA failed the increment | 2 | architect revises the plan (PLANNING) |
| `crashes.<role>` | session did not reach an artifact (timeout, non-zero exit) | 1 retry | BLOCKED |
| `env_attempts` | environment repair attempts | 2 | AWAITING_HUMAN |
| `total_rejections` | fuse for the whole increment | 6 | AWAITING_HUMAN |

Reset rules: counters reset on a new cycle; on a plan revision or `hv reopen`
the per-role rejections reset but `total_rejections` **does not** — otherwise
the fuse can be walked around with one more plan revision. A plain stage retry
never resets anything.

Crashes never consume rejection counters. A timeout is not a quality verdict.

## 7. Criteria: changeable, not rewritable

The architect may change criteria when circumstances change. The change is a
**new version**, never an edit in place:

* every criterion keeps `id` and `version`; old versions stay in
  `acceptance_history`;
* changing the increment's `criteria_version` mid-cycle puts the cycle in
  `reopened`: dev and QA re-run against v+1, counters reset (the fuse does not);
* every change is written to `DECISIONS.md` with its reason and is counted;
  high criteria churn is a signal that the plan is wrong, not the code;
* an artifact whose `criteria_version` or `plan_sha` disagrees with the plan is
  rejected, and at acceptance a QA artifact from another plan blocks the merge.

Unversioned rewriting is detected mechanically: the dispatcher stores a
snapshot of `text_sha`/`check_sha` per criterion at planning time and compares
it at acceptance. Rewriting text while keeping the version is an audit-trail
defect and stops the cycle.

## 8. Deviations and manifests

The developer report has a mandatory `Deviations and implicit decisions`
section. Anything decided outside the plan belongs there; an undeclared
deviation is a defect even when the code works.

Dependency manifests (`pyproject.toml`, lock files, `package.json`, `go.mod`,
…) are special: the developer may change them, but only as an explicit
deviation, and the dispatcher then re-runs `make setup` in a cold container.
Cold setup fails → artifact rejected automatically. `Makefile`, `Dockerfile`
and toolchain versions stay with the enabling-engineer.

## 9. Gates

```
setup    make setup      (cold in ENV and after a manifest change)
build    make build
test     make test
lint     make lint
regression: build + test + lint on the merged result, at every cycle boundary
```

An empty command means the gate does not exist for this project and shows as
`—` in the report. A gate result is stored as `pass|fail|timeout|skip|missing`
plus a tail of the output; test gates also record `passed/total` when it can be
parsed.

The regression gate is the only protection against drift accumulating between
cycles: QA checks the increment, nobody checks that everything before it still
works.

## 10. Merge policy

* `cycle/NN` is cut from `main`; `main` receives a merge only at acceptance and
  only after the regression gate on the merged result — so `main` is always
  green;
* the dispatcher merges, never an agent. The root checkout must be clean and on
  `main`; otherwise the cycle is BLOCKED (a dirty working tree is not a merge
  conflict, it is a reason to stop);
* WIP commits never reach `main`: at acceptance the cycle branch is squashed
  into one commit, then merged with `--no-ff`;
* accepted cycle → tag `cycle-NN-accepted`, branch kept;
* abandoned cycle → tag `cycle-NN-abandoned`, branch deleted, commits survive
  under the tag; a retry starts as `cycle/NN-a2`.

## 11. Human interface

The human boundary is a **machine state**, not a chat line: `AWAITING_HUMAN`
with `reason` and a list of legal options, or `BLOCKED` with a cause.

Order is mandatory: **write the decision first, then change the state.**

```
hv decide --kind reject|reopen|criteria-change|accept|note --text "..."
hv resolve --decision D-003 --stage IDLE
hv approve --decision D-003
hv reject  --decision D-003 --reason "..."
hv reopen  --criteria 4 --decision D-003
```

`resolve`, `approve`, `reject` and `reopen` refuse to run without an existing
decision id. The window between "the human said" and "it is written" is exactly
the window in which a project transfer loses the decision.

The hypervisor session is one-shot as well: `/new` plus `hv status` must
restore the picture. Its legal functions are turning state into the fixed-shape
report, keeping metrics (criteria churn, retries per stage, cost per role,
cycle time), raising smells, and batching human decisions to cycle boundaries.

## 12. Adapter

`run_agent_cli(role, prompt, workdir, timeout)` spawns a child process. Never a
subagent of the watching session: closing the TUI would kill the work.

`hv.toml`:

```toml
[adapter]
cmd = 'hermes -z "$(cat {prompt_file})" --usage-file {usage_file}'
timeout = 3600
```

Placeholders: `{prompt_file}`, `{workdir}`, `{role}`, `{cycle}`, `{attempt}`,
`{usage_file}`, `{project}`, `{root}`, `{skill_root}`. The command runs through
`sh -lc`. Exported to the child: `HV_ROLE`, `HV_STAGE`, `HV_CYCLE`,
`HV_ATTEMPT`, `HV_INCREMENT`, `HV_PLAN_SHA`, `HV_CRITERIA_VERSION`,
`HV_PROJECT`, `HV_USAGE_FILE`.

Known-good command lines (the first is tested on the author's host; the others
are examples — verify flags against your CLI version):

```toml
# Hermes Agent, one-shot, with a usage report
cmd = 'hermes -z "$(cat {prompt_file})" --usage-file {usage_file}'

# Qwen Code, non-interactive
cmd = 'qwen -y -o text "$(cat {prompt_file})"'

# Claude Code
cmd = 'claude -p "$(cat {prompt_file})" --dangerously-skip-permissions'

# Codex CLI
cmd = 'codex exec --full-auto "$(cat {prompt_file})"'

# The deterministic fake used by the tests
cmd = 'FAKE_MODE=ok python3 {skill_root}/examples/adapters/fake.py {prompt_file}'
```

Cost is collected by the dispatcher, never reported by a role: from the usage
file when the CLI writes one, otherwise from a `HV_USAGE {...}` JSON line at the
end of the session log, otherwise from a `run/cost/<session>.usage.json`.
`cost.json` per cycle is committed with the reports so the comparison base
survives a transfer.

## 13. Isolation

A headless agent with write access and `make` on the host sees `~/.ssh`, git
credentials and the network. With `isolation.mode = "docker"`:

* only the cycle worktree is mounted (rw), at `/work`; the home directory is
  never mounted;
* no ssh keys and no git credentials inside: pushing is physically impossible,
  and git operations belong to the dispatcher;
* `--network none` by default; dependency installation is the enabling-engineer's
  `setup`, not a runtime action of the developer;
* CPU, memory and time limits; `--yolo`-style auto-approval is only acceptable
  inside the container;
* stdout/stderr land in `run/logs/` — both the debug trail and the cost source.

When the docker daemon is unreachable, `hv` degrades to `isolation=none` with a
warning and records the fact in STATE, because a missing socket must not look
like a working sandbox. On such a host the cold-container gate reports `skip`
and the cycle report says so.

## 14. Transfer to another machine

`STATE` is an index, not the source of truth: meaning lives in git (plan,
`DECISIONS.md`, reports with attempt numbers) and `run/` is rebuilt.

* `git bundle create project.bundle --all` (or `git push --all`): a plain
  `git clone` creates only remote-tracking branches, which is an easy way to
  lose local ones;
* worktrees do not travel — their metadata under `.git/worktrees` is tied to
  paths. Branches travel, working copies are recreated;
* `run/` stays in `.gitignore`; on the new machine `hv rebuild` recreates it
  and the `.hv/spec` checkout;
* check the transfer with `hv rebuild` → `hv gate`: same picture or the schema
  is wrong, not the transfer;
* metrics travel separately from the raw log: `cost.json` per cycle is
  committed, `log.jsonl` stays local.

## 15. Dispatcher responsibilities

1. read and write `STATE`, append to `log.jsonl`;
2. render the session prompt: role contract + the increment pinned by
   `plan_sha` + the previous artifact. The prompt must not restate the code —
   the agent reads the worktree itself;
3. validate artifacts by schema (sections, criterion ids, deviation list): one
   retry, then escalate;
4. run gates: setup, build, test, lint, cold setup, regression;
5. route transitions, keep counters, freeze and unfreeze the plan, create the
   cycle worktree;
6. on every session exit — success, rejection or timeout — commit the working
   copy (`wip/<role>-cNN-aK`) so an interrupted session resumes from real state.

What the dispatcher must never do: judge quality, edit the plan, write
`DECISIONS.md`, fix code.

## 16. State machine

```
IDLE → PLANNING → ENV → DEV → QA → ACCEPT → (next cycle) → DONE
                 ↘ AWAITING_HUMAN(reason) ↗
                 ↘ BLOCKED(reason)
```

`AWAITING_HUMAN` is not a dead end: `hv resolve` returns to any stage with a
recorded decision. `BLOCKED` marks a broken precondition (merge conflict, dirty
main, infrastructure) — a human fixes the cause, `hv rebuild` re-derives state.

`STATE.json` (schema 1):

```json
{
  "schema": 1,
  "project": "name",
  "cycle": 4,
  "phase_id": "2.1",
  "stage": "QA",
  "attempt": {"env": 1, "dev": 2, "qa": 1, "architect": 3},
  "counters": {
    "rejections": {"dev": 1, "qa": 0},
    "crashes": {"env": 0, "dev": 0, "qa": 0, "architect": 0},
    "env_attempts": 0,
    "total_rejections": 1,
    "artifact_retries": {"env": 0, "dev": 0, "qa": 0, "architect": 0}
  },
  "plan_sha": "…64 hex…",
  "plan_ver": 7,
  "criteria_version": 3,
  "criteria_snapshot": {"C-2.1.1": {"version": 3, "text_sha": "…", "check_sha": "…"}},
  "env_sha": "…tree hash…",
  "branch": "cycle/04",
  "worktree": "/abs/worktrees/cycle-04",
  "gates": {"build": "pass", "test": "41/41", "lint": "pass", "regression": "pass"},
  "isolation": "docker",
  "awaiting": {"reason": "…", "options": ["…"]},
  "reopened": false,
  "reopen_count": 1,
  "cost": {"total_usd": 0.42, "sessions": []},
  "history": []
}
```

## 17. CLI contract

```
hv init   --adapter-cmd '...' [--isolation docker|none] [--name X] [--force]
hv status [--json]              where we are, what is blocked, what waits
hv plan                         increments, criteria versions, pins
hv run    [--until blocked|cycle|accept|done] [--max-cycles N]
hv report [cycle]               fixed-shape cycle report
hv gate   [--cold]              deterministic gates
hv spawn  <role>                one session: render, run, collect
hv decide --kind ... --text "..."   append a decision (the only way in)
hv resolve --decision D-NNN [--stage S]
hv approve --decision D-NNN [cycle]
hv reject  --decision D-NNN [cycle] --reason "..."
hv reopen  [--criteria N] --decision D-NNN
hv rebuild                      re-derive STATE from git
hv log [--tail N]               append-only dispatcher log
hv version
```

`hv run` returns control to the hypervisor only at significant boundaries: a
closed cycle, an attempt limit, a gate failure, a question that needs a human.
Everything else the agent must not see.

Exit codes: `0` ok, `2` blocked or refused (with a reason on stdout).

## 18. Definition of done

- [x] `hv run` completes a whole toy project (two increments, ~15 files) with no
      human in the loop
- [x] a session that times out resumes from a checkpoint commit
- [x] `hv status` after a fresh process restores the picture
- [x] a criteria bump mid-cycle reopens the cycle and restarts dev/QA
- [x] two QA failures escalate to the architect, the fuse escalates to a human
- [x] an artifact without mandatory sections is rejected and retried once
- [x] artifacts with a foreign `plan_sha` block acceptance
- [x] `hv rebuild` on a fresh clone reproduces `STATE`, and `hv gate` reproduces
      the result
- [x] `git bundle --all` transfer works without editing paths or worktrees
- [x] the container mount set is the cycle worktree only — no home, no ssh, no
      network
- [x] a rejected cycle leaves a tag, not a branch
- [x] `main` is green after acceptance (regression gate on the merge result)
- [x] a timeout does not consume rejections; a second crash blocks the cycle
- [x] a manifest change without a successful cold build rejects the artifact
- [x] `main` contains neither `PLAN.md` nor `env/`
