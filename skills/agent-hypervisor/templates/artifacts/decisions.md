# DECISIONS

Append-only. One entry per decision, newest last. Written by `hv decide`
(human) or by the architect on a cycle boundary; existing entries are never
edited or deleted.

## D-001
- ts: 2026-01-01T00:00:00+00:00
- actor: architect
- kind: criteria-change
- cycle: 2
- plan_sha: 0123456789ab
- text: C-2.4 relaxed - check moved from smoke to unit because the external service is unavailable in CI
