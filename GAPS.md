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

## Tracked implementation gaps

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

### 3. Retrieval quality and coverage

Still missing:

- bounded iterative retrieval when evidence is incomplete;
- explicit source freshness requirements beyond the current active-record
  preference;
- retrieval Recall@k/MRR and policy-quality benchmarks; and
- meaningful chunking and evaluation of long runbooks rather than relying
  primarily on one vector per document.

#### What Recall@k and MRR measure

These metrics evaluate whether retrieval returns the evidence that an
investigation needs, and how high that evidence appears in the ranked results.
They require a benchmark set of representative queries with the relevant,
authorized evidence identified in advance.

**Recall@k** asks: of all the evidence records marked relevant for a query,
what fraction appeared in the first *k* results? If a query has four relevant
records and the top five results include three of them, Recall@5 is 3/4, or
75%. A higher value means fewer known relevant records were missed within the
result limit. The benchmark averages this fraction across its queries.

**Mean Reciprocal Rank (MRR)** asks how early the first relevant result
appears. For each query, score the rank of its first relevant result as
`1 / rank`: a first result at rank 1 scores 1, at rank 2 scores 1/2, and at
rank 5 scores 1/5. If no relevant record appears, the query scores 0. MRR is
the average of these scores across queries, so it rewards putting useful
evidence near the top.

For LedgerFlow, benchmark queries should have labeled incident, invoice, log,
deployment, and runbook evidence IDs. Calculate the metrics over the actual
ranked retrieval output for each task policy, and report results separately by
policy and source type. Labels and scoring must respect tenant scope: an
unauthorized cross-tenant record must never count as a relevant hit. Recall@k
shows coverage at the configured evidence limit; MRR shows ranking quality for
the earliest useful result. Neither metric alone proves that a report's
conclusions are correct, so pair them with citation correctness and
claim-level evaluation.

### 4. Memory lifecycle and verified-fact management

AgentCore Memory is provisioned with expiration settings and the runtime can retrieve records and create events. The deployed investigation path does not yet provide a complete lifecycle for verified facts. The separate DynamoDB verified-facts table is also not the source of truth for the cloud runtime.

Still missing:

- explicit fact creation, deduplication, supersession, and conflict handling;
- provenance and evidence links for every persisted fact;
- active-versus-expired filtering in retrieval;
- a policy for what may be promoted from an investigation into memory;
- tests for stale, contradictory, and cross-tenant memories;
- a seeded memory corpus that is actually exercised by the deployed path.

A future canonical verified-facts ledger should explicitly record provenance and
lifecycle. The proposed record fields are:

```text
memory_id, tenant_id, subject_id, fact_key, fact_value, source_ids,
authority, confidence, observed_at, expires_at, status, supersedes
```

AgentCore Memory can provide semantic recall, while this ledger should be the
authoritative source for verified facts, supersession, and expiration. The
current Runtime does not yet use the verified-facts table as that source of
truth.

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


The planned benchmark uses 20 investigations covering duplicate invoices,
payment timeouts, configuration failures, outdated runbooks, cross-tenant
adversarial documents, superseded memories, prompt-injection tool output,
incomplete incidents, expired approvals, and simulated worker crashes.
Measure retrieval Recall@5 and MRR, citation correctness, root-cause accuracy,
unauthorized-record exclusion, memory freshness, invariant retention, tokens
per verified report, cache-eligible and cached-token ratios, recovery rate,
duplicate-operation rate, end-to-end latency, and cost per verified report.
Use deterministic evaluators for authorization, citations, invariants, recovery,
and duplicate operations; use AgentCore Evaluations for report completeness,
reasoning quality, and clarity.

### 7. Policy enforcement in deployed environments

The backend tools enforce a read-only, tenant-scoped boundary, and the CloudFormation defaults now use `ENFORCE` for Gateway policy mode and `ACTIVE` for policy enforcement. Deployments may still explicitly select `LOG_ONLY` during a controlled rollout.

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

## Productionization gaps and readiness gates

The POC is production-shaped, but it is not yet ready for unrestricted
customer data or autonomous operational action. Productionization should be
treated as a separate readiness track: prove the security, data, quality,
reliability, and operating controls around the existing workflow before
expanding its scope.

### 1. Product risk and operating model

The first production release should be a read-only decision-support tool with
human review. It should not issue refunds, credits, cancellations, invoice
changes, or other financial writes.

Still missing:

- a signed definition of what the report is allowed to decide, recommend, and
  publish;
- human-review and escalation rules for low-confidence, conflicting, or
  high-impact findings;
- service-level objectives for latency, availability, report freshness, and
  recovery;
- documented RTO/RPO, retention, deletion, and data-residency requirements;
- an owner for each investigation, an operational support process, and an
  incident-response runbook; and
- a risk and compliance assessment for the real financial and customer data.

### 2. Identity, tenant isolation, and boundary hardening

The tenant boundary must be enforced by authenticated identity and backend
policy, never by model output or an untrusted request field. Every tool target
must independently validate the principal, tenant, customer, and evidence
scope.

Still missing or requiring production hardening:

- enterprise federation, MFA, user lifecycle management, and a controlled
  break-glass administrator path;
- tests proving that a caller or retrieved document cannot select another
  tenant or escalate tool permissions;
- Gateway-only Runtime invocation so direct Runtime access cannot bypass
  Gateway policy, guardrails, or interceptors;
- an approved VPC/PrivateLink network design, or a documented reason that
  public Runtime networking is acceptable;
- verification of current AgentCore Runtime security requirements, including
  MMDSv2, least-privilege execution roles, non-root containers, and resource
  policies;
- backend and IAM gating for Dev tools, not only UI visibility; and
- a production CORS allow-list, WAF protection, API throttling, and request
  size/rate limits.

Repository-specific checks include the `PUBLIC` Runtime network mode in
`cloudformation/stacks/04-agentcore.yaml` and the `*` default for
`AllowedOrigin` in `cloudformation/stacks/07-web-app.yaml`. These defaults
must be explicit, reviewed production parameters and should fail deployment
when left unsafe.

### 3. Data protection, retention, and disaster recovery

Synthetic fixtures hide the consequences of exposing real invoices, logs,
identifiers, and operational history. Production data needs a complete
classification and lifecycle policy.

Still missing:

- PII/PCI-sensitive field minimization, redaction, masking, and access review;
- separate production accounts, keys, buckets, indexes, and prompt resources
  from development environments;
- customer-managed encryption and key-rotation procedures, with tightly
  scoped decrypt access;
- PITR and tested restore procedures for every critical DynamoDB table,
  including cross-Region recovery where required;
- report and audit retention with immutable storage or legal holds where
  required;
- evidence snapshots, hashes, timestamps, and source versions so a report can
  be reproduced after source records change; and
- tested deletion and subject-data export workflows that include memory,
  vectors, reports, logs, and cached content.

### 4. AI quality, safety, and evaluation

The deterministic contract audit proves structural validity, not that a report
is correct. Production approval needs a repeatable quality gate.

Still missing:

- a golden corpus covering every supported incident type and failure mode;
- retrieval Recall@k/MRR, citation correctness, evidence-claim accuracy,
  unsupported-claim rate, confidence calibration, and user-acceptance metrics;
- adversarial fixtures containing prompt injection, poisoned runbooks, malformed
  records, conflicting evidence, stale memory, and cross-tenant content;
- evaluation of the full report, not only planner and schema validity;
- regression thresholds enforced in CI and before model, prompt, retrieval, or
  schema releases; and
- layered prompt-attack and content controls such as Bedrock Guardrails,
  while keeping authorization and evidence validation deterministic outside the
  model.

### 5. Prompt, model, and memory governance

Prompt Management and Memory are useful production controls only when their
lifecycles are governed.

Still missing:

- approval, review, canary, rollback, and change records for prompt and model
  activation;
- report lineage containing model ID, prompt version, retrieval policy,
  context-manifest version, and evidence snapshot identifiers;
- a formal memory promotion policy with deduplication, supersession, conflict
  handling, provenance, freshness, and tenant-scoped deletion;
- separate draft editing from production activation; and
- provider-accurate token, latency, cache-hit, and cost accounting for every
  model stage.

The current admin Prompt library can edit drafts and create versions. A
production release should require an approved change path before a new version
is selected for runtime use.

### 6. Durable workflow, scale, and recovery

The current checkpoint and lease design demonstrates a strong single-run
foundation, but production failures will include worker termination, provider
timeouts, duplicate submissions, partial batches, hot tenants, and schema
migrations.

Still missing:

- fault-injection tests at every meaningful checkpoint and for concurrent
  resume;
- explicit handling and reconciliation for uncertain external calls;
- checkpoint schema versioning, migration, and incompatible-version behavior;
- bounded concurrency, per-tenant quotas, backpressure, and model/tool rate
  limits;
- a batch coordinator with per-customer state, resumable partial completion,
  and operator views for pending, failed, retried, and completed work;
- load tests against realistic evidence sizes and model quotas; and
- recovery procedures that do not depend solely on the Step Functions redrive
  window or execution-history limits.

### 7. Observability, audit, and cost controls

The current dashboard and failure alarm are a starting point, not an
operations system.

Still missing:

- end-to-end correlation across user, tenant, workflow, tool, model, prompt,
  checkpoint, and report IDs;
- SLO dashboards and alarms for success rate, latency, queue depth, retries,
  lease expiry, policy denials, citation failures, and stale data;
- per-tenant and per-stage token, model, retrieval, and infrastructure cost
  attribution;
- budgets, quotas, throttles, and abuse detection for billable investigations;
- centralized CloudTrail, security findings, and access reviews; and
- a deliberate policy for Bedrock invocation logging because model inputs and
  outputs may contain sensitive data.

### 8. Delivery, supply chain, and environment management

The current deployment is intentionally single-account and single-Region. A
production service needs controlled promotion and rollback.

Still missing:

- separate dev, staging, and production accounts with distinct IAM and KMS
  boundaries;
- CI/CD gates for unit, contract, security, evaluation, load, and restore
  tests;
- immutable ECR image digests, SBOM generation, dependency scanning, image
  signing, and base-image patching;
- CloudFormation change approval, drift detection, and tested rollback;
- canary or blue/green Runtime, workflow, prompt, and model releases; and
- versioned configuration with no secrets in templates, logs, deployment
  context, or container environment variables.

### 9. Production pilot exit criteria

Before enabling broad customer access, require all of the following:

- read-only workflows with human sign-off;
- enforced tenant and Gateway policy tests in a non-development environment;
- a passing golden-case and adversarial evaluation suite;
- tested backup restore, checkpoint recovery, and partial-batch recovery;
- defined SLOs, dashboards, alarms, quotas, and on-call ownership;
- reviewed data retention, deletion, audit, and compliance controls; and
- a limited pilot using anonymized or explicitly approved production data.

Authorized financial operations should remain a later phase. If they are ever
added, use the safeguards in Gap 10 plus a dry-run, reconciliation, approval
expiry, and independent execution audit.

## Suggested learning order

1. **Token budgeting and caching** — next.
2. Retrieval quality and coverage.
3. Memory lifecycle.
4. Recovery depth.
5. Adversarial and authorization tests.
6. Policy enforcement.
7. Evaluation and benchmarks.
8. Batch and final production simulation.
9. Authorized operations and approval safeguards.

Authorized operations are deliberately last because they introduce irreversible side effects. The preceding topics teach the remaining agent-engineering lessons while preserving the current read-only safety boundary.
