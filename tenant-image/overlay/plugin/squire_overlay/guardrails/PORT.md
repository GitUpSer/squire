# Founder guardrail port

Ported from `hermes-agent-bootstrap` commit
`f8a07eb5352b53e9fb97d30eca2c456ab47f0240`,
`overlay/plugin/hermes_personal/guardrails`. The source repository and running
founder service are unchanged.

The browser lease, deadline, owned process/daemon cleanup, breaker, findings,
preflight and progress ledger retain the original algorithms and regression tests.
Adaptations:

- One runtime/container is one conversation and tenant; no cross-tenant browser.
- Systemd user scopes are disabled. The container is the outer resource boundary.
- Only private runtimes register these modules; group tools remain restricted.
- Progress uses durable broker egress scoped to the immutable private conversation.
- `/guardrails` status/recovery requires that private Squire conversation.
- Actual Hermes 9.24 browser spawn seam remains mandatory for owned cleanup.

The port does not claim systemd per-workload cgroup isolation inside a Railway
container. Process watchdog limits and container resource limits are separate
controls; their coverage and failure modes must be tested explicitly.
