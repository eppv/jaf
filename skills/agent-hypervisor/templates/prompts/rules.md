## Rules of engagement

- Work only inside the workdir above; the spec copy is the contract.
- Nothing is remembered between attempts: the artifacts are the state.
- Do not edit files outside your role's ownership; the dispatcher checks the
  spec copy and rejects edits made by a non-owner.
- State changes go through the dispatcher, never by hand.
