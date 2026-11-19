# Implementation Gaps

## The most important gap for learning

The highest-value learning project is **context compaction with invariant preservation**.

It exercises the central problem in a reliable agent: how to reduce a long-running investigation into a smaller context without losing the rules and facts that make the result safe. A useful implementation would have to preserve, validate, and recover at least these invariants:

- tenant and authorization boundaries;
- evidence IDs, source provenance, and claim types;
- workflow state, checkpoints, and operation IDs;
- unresolved questions and uncertainty;
- the distinction between observed facts and model hypotheses.

This is more educational than starting with a cost optimization because it connects prompting, retrieval, memory, workflow recovery, and safety in one end-to-end exercise. It also exposes whether the agent can keep making correct decisions after many steps rather than only producing a good report from a short context.

The recommended first implementation is a structured, token-aware compactor with an invariant manifest and tests that compact the same investigation repeatedly, then verify that the resulting context still produces the same safety decisions and evidence citations.

## Current baseline

The repository already has a deployable AWS vertical slice with AgentCore, Bedrock, Gateway, Step Functions, DynamoDB, S3, and S3 Vectors. It also has hybrid tenant-scoped retrieval, native Bedrock prompt versions, typed evidence claims under `ledgerflow.evidence.v2`, durable checkpoints, leases, idempotent report publication, and a read-only tool boundary.

The items below are gaps against the larger case-study target and useful learning opportunities. They are not all defects in the current proof of concept.

## Ten gaps

### 1. Context compaction and invariant preservation

The runtime currently limits the model packet with character and item-count truncation in `ledgerflow/reasoning.py`. That is not token-aware compaction and does not maintain a formal invariant manifest.

Still missing:

- a token-budgeted compactor;
- structured summaries of facts, relationships, hypotheses, unresolved questions, and completed work;
- durable references back to source evidence rather than copying arbitrary text;
- invariant checks before and after compaction;
- repeated-compaction and long-investigation regression tests.

### 2. Token budgeting and caching

The runtime has prompt version selection, but prompt versioning is not the same as token budgeting or Bedrock prompt-prefix caching.

Still missing:

- tokenizer-based input and output budgets per workflow stage;
- a dynamic allocator for evidence, history, instructions, and report space;
- input-token, latency, and cost metrics;
- prompt-prefix cache configuration and cache hit-rate measurement;
- a benchmark showing the quality/cost trade-off.

### 3. Task-aware retrieval policies

The planner creates an investigation plan, while report synthesis currently uses a fixed investigation task type. Retrieval is hybrid and tenant-scoped, but the policy is not selected from the user’s task.

Still missing:

- task classification for investigation, explanation, verification, and any future operation request;
- a policy registry describing allowed sources, freshness requirements, ranking weights, and stopping conditions per task;
- bounded iterative retrieval when evidence is incomplete;
- policy-selection and retrieval-quality telemetry;
- meaningful chunking and evaluation of long runbooks rather than relying primarily on one vector per document.

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

1. Context compaction and invariant preservation.
2. Token budgeting and caching.
3. Task-aware retrieval policies.
4. Memory lifecycle.
5. Recovery depth.
6. Adversarial and authorization tests.
7. Policy enforcement.
8. Evaluation and benchmarks.
9. Batch and final production simulation.
10. Authorized operations and approval safeguards.

The tenth gap is deliberately last in the implementation order because it introduces irreversible side effects. The first nine gaps can teach most of the important agent-engineering lessons while preserving the current read-only safety boundary.
