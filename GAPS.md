# Implementation Gaps

## The next gap for learning

The next highest-value learning project is **token budgeting and caching**.

Context compaction now keeps a long investigation safe and bounded, but its
token count is still a deterministic estimate rather than provider-accurate
usage. The next lesson is how to allocate a finite model budget across
instructions, workflow history, evidence, semantic summaries, and output—and
then reduce repeated cost with Bedrock prompt-prefix caching.

This matters because a context can be safe and still be too expensive, too
slow, or too close to the model limit. The implementation should measure the
trade-off instead of treating the token budget as a single magic number:

- preserve mandatory instructions, citations, and safety state first;
- allocate remaining tokens by evidence value and task stage;
- record actual input/output tokens, latency, and estimated cost;
- reuse stable prompt prefixes when the provider supports it; and
- compare report quality, citation retention, latency, and cost before and
  after caching.

## Current baseline

The repository already has a deployable AWS vertical slice with AgentCore, Bedrock, Gateway, Step Functions, DynamoDB, S3, and S3 Vectors. It also has hybrid tenant-scoped retrieval, native Bedrock prompt versions, typed evidence claims under `ledgerflow.evidence.v2`, durable checkpoints, leases, idempotent report publication, a read-only tool boundary, and deployed context compaction with an optional Bedrock semantic pass.

The items below are gaps against the larger case-study target and useful learning opportunities. They are not all defects in the current proof of concept.

## Ten gaps

### 1. Context compaction and invariant preservation — complete

Implemented in `ledgerflow/context_compaction.py` and the deployed Runtime.
The deterministic compactor now preserves tenant and read-only invariants,
mandatory evidence citations, typed claims, workflow state, unresolved
questions, and bounded memory records. It emits a versioned context manifest,
and an optional Bedrock semantic pass can add cited observations,
relationships, hypotheses, and questions without becoming authoritative.

The `ACME-496` / `INC-1059` pressure case proves the behavior with 138 input
records: 18 full records, 120 citation-only records, and zero omitted records
under the default 8,000-token budget. The console exposes those results in the
timeline hover, Reasoning inspector, and Execution log. Regression coverage
now includes deterministic repeatability, cross-tenant rejection, citation
retention, and the pressure fixture.

Remaining work here is refinement rather than the original gap: provider
accurate token accounting belongs to Gap 2, and larger batch/recovery behavior
belongs to Gaps 5 and 8.

### 2. Token budgeting and caching — next

The runtime now has a deterministic estimate, a reserved-token budget, bounded
evidence selection, and compaction telemetry. That is enough to demonstrate
safe behavior, but it is not yet an operational token-management system.

Still missing:

- provider-accurate input and output token accounting for each Bedrock stage;
- a dynamic allocator for instructions, workflow history, evidence, semantic
  summaries, and report output;
- per-stage latency, token, and cost metrics in the execution view;
- Bedrock prompt-prefix cache configuration and cache-hit measurement; and
- a golden-case benchmark showing the quality/cost trade-off at several
  budgets.

The best first exercise is to instrument planner, compaction, and synthesis
with actual usage metadata, then compare a fixed 8,000-token run with smaller
budgets and a cache-enabled run. The benchmark should verify that citations,
typed claims, invariant flags, and report quality do not regress while token
consumption, latency, or cost improves. A cache hit must never allow stale
tenant evidence or an old prompt version to leak into a new request; the cache
key must include the active prompt version and the stable instruction prefix,
while request-specific evidence remains dynamic.

### 3. Task-aware retrieval policies — implemented

The planner now returns a validated `task_type`, and
`ledgerflow/retrieval_policies.py` maps it to a deterministic, read-only
retrieval recipe. The deployed console can let the planner classify a request
or accept an explicit investigation, explanation, or verification selection.

Implemented policies are:

- `investigation-v1`: structured incident, invoice, log, and deployment data;
  hybrid S3 Vectors retrieval; and verified memory;
- `explanation-v1`: incident/deployment context plus runbook-only,
  semantic-oriented evidence; and
- `verification-v1`: current structured records plus authoritative runbook
  corroboration, with an authority/freshness/identifier rerank.

The runtime records the selected policy, source scope, strategy, and limits in
the execution events, carries the task type into the report, and falls back to
the bounded investigation policy when planning is unavailable or invalid. The
tenant filter and read-only authorization remain independent of classification.

Still missing:

- bounded iterative retrieval when evidence is incomplete;
- explicit source freshness requirements beyond the current active-record
  preference;
- retrieval Recall@k/MRR and policy-quality benchmarks; and
- meaningful chunking and evaluation of long runbooks rather than relying
  primarily on one vector per document.

### Cross-cutting contract validation — implemented

The deployed workflow now validates the structured investigation contract in
two places. The AgentCore Runtime runs a deterministic gate immediately before
report publication. Step Functions then invokes the audit Lambda after the
Runtime returns; the Lambda rereads the durable checkpoint and event history,
reconstructs the evidence packet, and records a `post_run` audit event.

The checks cover task-policy selection, tenant scope, evidence schema, required
and authorized citations, typed claim references, excluded source scopes,
memory boundaries, and the read-only tool contract. The website exposes the
pre-publication gate and post-run audit as timeline stages and displays the
bounded result in the report and execution log.

This is deliberately not a Bedrock quality evaluation. It proves that the
workflow obeyed deterministic structural contracts; it does not judge whether
an LLM hypothesis is correct. The remaining evaluation gap is to measure
quality, retrieval accuracy, cost, latency, and recovery across a golden-case
corpus rather than only validating one run's structure.

### 4. Memory lifecycle and verified-fact management

AgentCore Memory is provisioned with expiration settings and the runtime can retrieve records and create events. The deployed investigation path does not yet provide a complete lifecycle for verified facts. The separate DynamoDB verified-facts table is also not the source of truth for the cloud runtime.

Still missing:

- explicit fact creation, deduplication, supersession, and conflict handling;
- provenance and evidence links for every persisted fact;
- active-versus-expired filtering in retrieval;
- a policy for what may be promoted from an investigation into memory;
- tests for stale, contradictory, and cross-tenant memories;
- a seeded memory corpus that is actually exercised by the deployed path.

### 5. Recovery depth

The implementation already demonstrates checkpoints, leases, a deterministic post-checkpoint failure, Step Functions retry, and idempotent publication. That is a strong single-run foundation.

Still missing:

- tests that terminate a worker or process at each meaningful checkpoint;
- batch recovery for a larger case set, including partial completion;
- an explicit pending-operation state for uncertain external calls;
- reconciliation of an operation whose result is unknown after a timeout;
- expired-approval recovery;
- checkpoint migration or an explicit reject path when the schema changes;
- stronger concurrent-resume regression tests.

### 6. Evaluation and benchmarks

The repository has functional tests, but it does not yet have an evaluation suite that measures whether the agent is improving or regressing.

Still missing:

- retrieval Recall@k and MRR;
- citation correctness and evidence-claim accuracy;
- memory precision, freshness, and conflict metrics;
- invariant-retention scores after compaction;
- cost and latency by workflow stage;
- recovery success and duplicate-operation rates;
- a repeatable golden-case benchmark run in CI or a deployment check.

### 7. Policy enforcement in deployed environments

The backend tools enforce a read-only, tenant-scoped boundary, but the deployed development configuration currently uses `LOG_ONLY` for Gateway and policy enforcement. That observes decisions without blocking them.

Still missing:

- an enforced mode tested in a non-development environment;
- explicit deny-path tests for unauthorized tenants, tools, and evidence;
- policy decision records tied to operation IDs;
- a safe rollout mechanism from logging to enforcement;
- monitoring for policy drift and unexpected allow decisions.

### 8. Final production simulation and batch scope

The workflow is centered on one customer and selects a single incident for that investigation. It does not yet model the case-study scenario where a deployment may affect many customers and the system must coordinate a larger run.

Still missing:

- a batch coordinator and per-customer state;
- bounded concurrency and rate limiting;
- resumable partial-batch checkpoints;
- aggregation of cross-customer evidence without violating tenant boundaries;
- a deterministic 63-of-100 style production simulation;
- an operator view of pending, failed, retried, and completed cases.

### 9. Adversarial and authorization tests

Tenant filtering and untrusted-evidence handling exist in the retrieval and reasoning path. The repository does not yet systematically test the attacks described by the case study.

Still missing:

- cross-tenant document and memory fixtures;
- prompt-injection documents that attempt to remove restrictions or request financial actions;
- tests proving that retrieved text cannot change tool authorization;
- malformed, conflicting, and poisoned evidence cases;
- measured authorization and evidence-grounding results rather than only happy-path assertions.

### 10. Authorized operations and approval safeguards

The current design intentionally keeps all tools read-only. It therefore does not implement the case-study extension for authorized low-risk operations.

Still missing, if that scope is added:

- a typed operation proposal separate from execution;
- explicit approval tokens and approver identity;
- scope, tenant, amount, and expiration checks;
- idempotency keys and an operation ledger;
- a dry-run and reconciliation path;
- audit events for proposal, approval, execution, denial, and uncertain outcome.

This should be implemented last among the ten. The read-only boundary should remain the default until the approval and reconciliation model is complete.

## Suggested learning order

1. ~~Context compaction and invariant preservation~~ — complete.
2. **Token budgeting and caching** — next.
3. ~~Task-aware retrieval policies~~ — implemented; iterative retrieval and
   evaluation remain open sub-gaps.
4. Memory lifecycle.
5. Recovery depth.
6. Adversarial and authorization tests.
7. Policy enforcement.
8. Evaluation and benchmarks.
9. Batch and final production simulation.
10. Authorized operations and approval safeguards.

The tenth gap is deliberately last in the implementation order because it introduces irreversible side effects. The first nine gaps can teach most of the important agent-engineering lessons while preserving the current read-only safety boundary.
