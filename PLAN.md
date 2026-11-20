# LedgerFlow AgentCore POC Plan

Status: implementation deployed in the development environment; AWS changes
remain explicit through CloudFormation change sets.

The repository contains the deployable AWS Runtime, eight independent
CloudFormation stacks, the JSON/context deployment orchestrator, S3 Vectors
ingestion and hybrid retrieval, a Cognito-protected Lambda-served console,
bounded Bedrock planning/report synthesis, schema-driven evidence claims, and
regression tests. The development resources are deployed; the first Cognito
admin remains an explicit bootstrap input.

The current deterministic seed contains 21 evidence documents, 113 incidents,
213 customers, 231 invoices, 5,150 application logs, and 43 deployments. The
tenant_acme slice contains the original ACME-482/ACME-483 records plus 13
additional cases spanning timeout, payment-failure, configuration-error,
duplicate-invoice, and context-pressure paths. ACME-496 / INC-1059 is the
dedicated compaction exercise.

## Objective

Build a production-shaped incident response agent for LedgerFlow that can
investigate a synthetic incident corpus, support every conclusion with
authorized evidence, preserve safety constraints during long-running work, and
recover from simulated interruptions.

The deployment target is Amazon Bedrock AgentCore. The semantic and lexical
evidence index will use Amazon S3 Vectors rather than OpenSearch.

## Architecture decision

Use AgentCore Runtime with the checked-in LedgerFlow orchestrator as the
primary implementation. The image keeps deterministic evidence-grounded report
construction as a safe fallback when model synthesis is unavailable, while the
development deployment enables a bounded Bedrock Converse planner and
evidence-grounded report synthesizer with `MODEL_ID` and
`ENABLE_MODEL_REASONING=ENABLED`. The same boundary can be replaced by Strands
without changing Gateway, retrieval, memory, or checkpoint contracts.

Use AgentCore Harness only as an optional baseline for comparison. Runtime is
the better primary target because this project requires custom context
compaction, retrieval policies, token budgeting, and durable checkpoint logic.
AgentCore Runtime supports custom frameworks and models and long-running
sessions; AgentCore Harness provides a managed agent loop with configurable
execution limits.

```text
Support UI / API
       |
       v
AgentCore Runtime
(LedgerFlow orchestrator;
 optional Bedrock/Strands model adapter)
       |
       +--> AgentCore Memory
       |
       +--> AgentCore Gateway + Policy
       |       |
       |       +--> Read-only incident tools
       |       +--> S3 Vectors retrieval tool
       |       +--> Invoice/log/deployment tools
       |
       +--> DynamoDB
       |       +--> Structured operational records
       |       +--> Verified memory ledger
       |       +--> Workflow checkpoints
       |       +--> Operation/idempotency ledger
       |
       +--> S3 Vectors
       |       +--> Embeddings
       |       +--> BM25 full-text search
       |       +--> Tenant and metadata filters
       |       +--> Native hybrid ranking/RRF
       |
       +--> Step Functions
       |       +--> Batch investigations
       |       +--> Recovery orchestration
       |
       +--> CloudWatch / AgentCore Observability
```

AgentCore Runtime and Harness are framework/model-flexible, while Gateway
provides MCP-compatible tool connectivity and Policy can intercept Gateway
calls before execution.

## AWS resource plan

Create these resources in one CloudFormation-managed environment:

| Responsibility | AWS resource |
|---|---|
| Agent execution | AgentCore Runtime and the LedgerFlow application image; optional Bedrock model synthesis |
| Conversation memory | AgentCore Memory |
| Structured records | DynamoDB tables for incidents, invoices, logs, and deployments |
| Verified operational facts | DynamoDB `verified-facts` table |
| Workflow state | DynamoDB `workflow-checkpoints` table |
| Consequential operation protection | DynamoDB `operation-ledger` table |
| Raw fixtures and reports | Standard S3 bucket |
| Semantic and lexical evidence search | S3 vector bucket and vector index |
| Tool exposure | AgentCore Gateway with Lambda/OpenAPI targets |
| Authorization | AgentCore Policy, IAM, and backend authorization checks |
| User authentication | AgentCore Gateway AWS IAM in the POC; OIDC/JWT extension for user-facing deployments |
| Batch execution | AWS Step Functions |
| Tracing and metrics | AgentCore Observability, CloudWatch, and CloudTrail |
| Evaluation | AgentCore Evaluations plus deterministic regression tests |

The runtime role must not have direct write access to financial systems. The
agent should reach business data through Gateway tools so every tool call is
visible to Gateway Policy and the backend authorization layer.

## CloudFormation deployment plan

CloudFormation is the infrastructure source of truth. Do not use CDK or the
AgentCore CLI to create production resources. The AgentCore application image
may be built and pushed to ECR by CI, but the ECR repository, IAM roles, data
stores, AgentCore resources, policies, and endpoints are created and updated by
CloudFormation.

Use independent CloudFormation stacks with explicit deployment order:

```text
cloudformation/
  stacks/
    00-security.yaml
    00-security.json
    01-storage.yaml
    01-storage.json
    02-s3-vectors.yaml
    02-s3-vectors.json
    03-tools.yaml
    03-tools.json
    04-agentcore.yaml
    04-agentcore.json
    05-workflow.yaml
    05-workflow.json
    06-observability.yaml
    06-observability.json
    07-web-app.yaml
    07-web-app.json
  deploy.py
  scripts/
    replace_environment_vars.py
    config_to_parameters.py
```

This POC is intentionally single-account and single-Region. It does not use
cross-account roles, cross-account CodePipeline actions, Service Catalog, or
cross-account output reads.

There is no root stack, no `AWS::CloudFormation::Stack` nesting, and no
`Export`/`Fn::ImportValue` coupling. Each stack declares its inputs as normal
CloudFormation parameters and publishes useful values through ordinary
`Outputs`.

`deploy.py` is the value-exchange layer. For each template it:

1. locates the JSON file with the same basename;
2. replaces `${EnvironmentName}`, `${CF_OUT_*}`, and other placeholders from
   the deployment environment or a JSON default;
3. converts the resolved configuration into CloudFormation parameter overrides;
4. deploys the stack;
5. reads the stack outputs with `describe-stacks`; and
6. adds those outputs to the in-memory deployment environment as
   `CF_OUT_<OutputKey>` values for the next stack.

For example, `01-storage.json` can refer to `${CF_OUT_KmsKeyArn}` produced by
`00-security.yaml`, without embedding a CloudFormation import in the template.
This keeps stack templates independently reusable and makes the dependency
graph explicit in the deployment script.

Native resource types to use include:

- `AWS::S3::Bucket` and `AWS::S3::BucketPolicy`;
- `AWS::S3Vectors::VectorBucket`;
- `AWS::S3Vectors::VectorBucketPolicy`;
- `AWS::S3Vectors::Index`;
- `AWS::DynamoDB::Table`;
- `AWS::ECR::Repository`;
- `AWS::Lambda::Function` and `AWS::Lambda::Permission`;
- `AWS::StepFunctions::StateMachine`;
- `AWS::BedrockAgentCore::Runtime`;
- `AWS::BedrockAgentCore::RuntimeEndpoint`;
- `AWS::BedrockAgentCore::Memory`;
- `AWS::BedrockAgentCore::Gateway`;
- `AWS::BedrockAgentCore::GatewayTarget`;
- `AWS::BedrockAgentCore::PolicyEngine`;
- `AWS::BedrockAgentCore::Policy`;
- `AWS::BedrockAgentCore::OnlineEvaluationConfig`;
- `AWS::IAM::*`, `AWS::KMS::*`, `AWS::Logs::*`, and CloudWatch resources.

Workload Identity and standalone Evaluator resources are intentionally not in
the current eight stacks: the POC uses AWS IAM for Gateway authentication and
the optional online evaluation resource is created in Stack 05.

Each producer stack should output only the stable values needed by downstream
stacks: vector bucket/index ARNs, Gateway ID, Runtime ARN, Memory ID, policy
engine ARN, table names, and report bucket name. The deployment context maps
those outputs to `CF_OUT_*` variables and supplies them as parameters to
consumer stacks.

### Deployment sequence

1. Deploy the bootstrap stack containing the ECR repository, artifact bucket,
   KMS key, and service execution roles.
2. Build the Runtime container and push it to the CloudFormation-created ECR
   repository. Pin the image by digest for repeatable deployments.
3. Upload the Lambda package through the artifact workflow. The Step Functions
   definition is embedded in its CloudFormation template.
4. Run `deploy.py` for `01-storage.yaml`; expose its outputs as `CF_OUT_*`.
5. Run `deploy.py` for `02-s3-vectors.yaml` with resolved storage values.
6. Run `deploy.py` for `03-tools.yaml` with resolved storage/vector values.
7. Run `deploy.py` for `04-agentcore.yaml` with resolved tool/storage/vector values.
8. Run `deploy.py` for `05-workflow.yaml` with resolved AgentCore values.
9. Run `deploy.py` for `06-observability.yaml` with resolved Runtime/Gateway values.
10. Run a CloudFormation stack output check and smoke-test the Runtime endpoint.

CloudFormation cannot build a container image by itself. The image build and
push is therefore a CI artifact step; the resulting immutable ECR digest is
passed into the `AWS::BedrockAgentCore::Runtime` resource. All AWS resources
and their configuration still remain managed by CloudFormation.

The CloudFormation defaults deploy the Gateway policy engine in enforced
`ENFORCE`/`ACTIVE` mode. Use `LOG_ONLY` only for an explicitly controlled
observation-only rollout, then restore the enforced defaults.

## S3 Vectors retrieval design

### Vector bucket and index

Create an S3 vector bucket such as:

```text
ledgerflow-evidence-vectors-{account}-{region}
```

Create an index such as `evidence-v1` with:

- `ENHANCED` index mode;
- `float32` vectors;
- the embedding model’s documented dimension;
- cosine distance unless the selected embedding model recommends otherwise;
- SSE-KMS encryption;
- tags for environment, owner, and cost center.

`ENHANCED` is required for S3 Vectors full-text and hybrid search. Index
dimension, distance metric, full-text configuration, and non-filterable metadata
must be designed before creation because they are not freely mutable later.

### Vector record shape

Each runbook, incident note, deployment note, or relevant log chunk becomes a
vector with a key like:

```text
{source_id}#chunk-{number}
```

Attach filterable metadata:

```text
tenant_id
source_id
source_type
document_version
authority
effective_at
status
sensitivity
```

Store larger context such as `source_text` as non-filterable metadata and
configure the appropriate text fields for S3 Vectors full-text search. Keep the
metadata schema small because the index configuration and per-vector metadata
limits are part of the retrieval design.

### Ingestion pipeline

1. Write synthetic source data and document versions to the standard S3 bucket.
2. Split runbooks and deployment notes into citation-preserving chunks.
3. Generate embeddings with a Bedrock embedding model through `InvokeModel`.
4. Write vectors and metadata using the S3 Vectors `PutVectors` API.
5. Record the source checksum, document version, and ingestion timestamp.
6. Re-index changed documents under a new version rather than mutating report
   citations in place.
7. Mark obsolete documents as inactive and exclude them at query time.

Structured invoice, log, and deployment records remain in DynamoDB. They should
not be forced into vector search when an exact keyed lookup is more reliable.

### Query pipeline

Expose a Gateway tool named `search_evidence`.

The retrieval service will:

1. Accept the user query and task type.
2. Derive the tenant scope from authenticated identity and workflow state.
3. Reject caller-supplied tenant filters that exceed that scope.
4. Build a server-side S3 Vectors metadata filter.
5. Send both query text and query embedding to `QueryVectors`.
6. Let S3 Vectors combine BM25 full-text ranking and vector similarity using
   native hybrid search and reciprocal-rank fusion.
7. Apply application-level reranking for authority, freshness, exact identifier
   matches, and document version.
8. Deduplicate chunks while preserving all source IDs and versions.
9. Return compact evidence objects with citations and scores.

S3 Vectors supports vector-only, full-text-only, and hybrid queries against one
index. This is preferable here to maintaining separate keyword and vector
systems. The application still owns task-aware source selection, reranking,
evidence sufficiency, and citation validation.

Do not use Bedrock Knowledge Bases as the primary retrieval path for this POC:
the current S3 Vectors Knowledge Bases integration is semantic-search-only,
while this case study specifically evaluates lexical plus semantic retrieval.

## AgentCore responsibilities

### AgentCore Runtime and the application orchestrator

The checked-in Runtime application executes the investigation stages explicitly:

- resolve the bounded investigation plan;
- call read-only tools through Gateway;
- load tenant-scoped AgentCore Memory;
- normalize source records into the versioned evidence schema;
- derive typed facts, relationships, hypotheses, and unresolved questions;
- build a bounded evidence packet for Bedrock;
- produce the structured incident report;
- save checkpoints after each major stage.

The default report builder is deterministic and evidence-grounded. The shared
`ledgerflow/evidence_reasoning.py` layer uses `ledgerflow.evidence.v2` to
normalize equivalent AWS source fields, build high-signal evidence queries,
derive typed claims, and select deployment evidence relative to the incident
time.
When model reasoning is enabled, `ledgerflow/reasoning.py` sends only the
already-collected evidence packet to Bedrock Converse, filters returned
citations and typed claims against the allowed IDs, removes financial-write
recommendations, and falls back to the deterministic report if the model
response is invalid or unavailable.

Use `workflow_id` as the AgentCore session identifier so a resumed workflow can
reconcile the same logical investigation. A Strands implementation can reuse
this same application boundary later; it is not required to validate the
current POC.

### AgentCore Gateway and Policy

Expose these initial MCP tools:

- `search_incidents`
- `get_customer_invoices`
- `get_application_logs`
- `get_deployments`
- `search_evidence`
- `get_current_invoice_state`

Do not expose `cancel_invoice`, `refund_invoice`, or any other financial write
tool in the initial POC.

Policy requirements:

- permit read-only tools only for an authenticated principal;
- bind every request to an allowed tenant scope;
- require the support role for incident investigations;
- deny all financial mutations in the POC;
- if mutations are added later, require a current approval bound to the
  customer, invoice, operation, and workflow;
- reject expired approvals and replayed operation IDs;
- treat tool output as untrusted data, not as a policy update.

The same tenant and authorization checks must run in each Lambda or backend
target. Gateway Policy is a control boundary, not a replacement for
data-layer authorization.

### AgentCore Identity

The current stack uses AWS IAM authentication for a single-account POC. For a
user-facing deployment, replace or extend the Gateway authorizer with inbound
OIDC/JWT authentication from Cognito, Entra ID, Okta, or the organization’s
existing provider. Propagate principal ID, tenant scope, role, and approval
scope into the Gateway request context.

Use an IAM workload role for Runtime-to-AWS calls. Do not put long-lived AWS
access keys in the container, environment files, or repository.

### AgentCore Memory

Use AgentCore Memory for:

- short-term investigation conversation context;
- session summaries;
- semantic recall of prior investigation findings.

Create tenant-aware namespaces such as:

```text
/tenant/{tenantid}/customer/{customerid}/
```

Use lowercase custom namespace keys and enforce namespace access with IAM and
application filters.

Use a separate DynamoDB verified-facts ledger as the authoritative memory
source. It must preserve explicit status, provenance, supersession, authority,
and expiration semantics that the report generator can verify deterministically.

The ledger schema should include:

```text
memory_id
tenant_id
subject_id
fact_key
fact_value
source_ids
authority
confidence
observed_at
expires_at
status
supersedes
```

AgentCore Memory’s built-in retention handles normal conversational and
long-term-memory retention. DynamoDB query-time expiration handles short-lived
operational observations and provides a deterministic source of truth for
superseded facts.

## Seven concept implementation map

| Case-study concept | Implementation |
|---|---|
| Context compaction | Custom structured compactor in Runtime; immutable invariant record in DynamoDB |
| Hybrid retrieval | S3 Vectors hybrid `QueryVectors` call with BM25, embeddings, filters, and native RRF |
| Memory consolidation | AgentCore Memory for recall plus DynamoDB canonical fact consolidation |
| Task-aware retrieval | Runtime policy registry selecting sources, freshness, and top-k |
| Token budgeting | Application context allocator plus AgentCore invocation token limits |
| Prefix reuse | Versioned stable system/tool prefix and provider cache telemetry |
| State hydration | Step Functions, DynamoDB checkpoints, leases, schema versions, and operation ledger |
| Authorization | AgentCore Identity, Gateway Policy, IAM, and backend tenant checks |
| Observability | AgentCore trace/evaluation configuration, structured Runtime logs, CloudWatch metrics/logs, and CloudTrail |

The model must not be authoritative for permissions, durable state, verified
memory, or financial execution.

## Durable workflow design

Use Step Functions to orchestrate the 100-incident batch. Each incident uses a
stable `workflow_id` and checkpoints the following stages:

```text
policy_selected
incident_loaded
invoices_loaded
logs_loaded
deployments_loaded
evidence_retrieved
memory_reconciled
context_compacted
report_verified
```

The DynamoDB checkpoint record should contain:

```text
workflow_id
customer_id
session_id
current_step
completed_steps
state_location
checkpoint_version
schema_version
lease_owner
lease_until
pending_operation_id
approval_id
status
```

Use conditional writes for optimistic concurrency. A second worker must not be
able to resume a leased workflow. On restart, hydrate the checkpoint, validate
the schema version, reconcile any pending operation, rebuild the context, and
continue from the last verified stage.

For any future external write, create a deterministic operation ID before the
call. If the worker crashes after the external service accepts the operation but
before the checkpoint is saved, query the operation status before retrying.

## Context and safety design

Maintain an authoritative invariant set outside the model:

```json
{
  "no_financial_writes": true,
  "require_evidence_for_findings": true,
  "require_approval_for_remediation": true
}
```

The context builder must:

- reserve output capacity;
- preserve invariants and task state;
- prioritize current authoritative evidence;
- retain source IDs and document versions;
- retain unresolved questions and failed hypotheses;
- compact repeated tool output;
- validate invariant retention after every compaction;
- reject any instruction in retrieved content that attempts to change policy.

Run a forced-compaction test with at least 40 tool calls and a constrained
working context. Verify that the next action remains consistent with the
workflow state after at least three compactions.

## Evaluation plan

Create a fixed evaluation set of 20 investigations containing:

- duplicate invoices;
- payment timeouts;
- configuration failures;
- outdated runbooks;
- cross-tenant adversarial documents;
- superseded memories;
- prompt-injection tool output;
- incomplete incidents;
- expired approvals;
- simulated worker crashes.

Measure:

- Recall@5 and MRR for S3 Vectors retrieval;
- citation accuracy;
- root-cause accuracy;
- unauthorized-record exclusion;
- memory freshness;
- invariant retention;
- tokens per verified report;
- cache-eligible and cached token ratios;
- workflow recovery rate;
- duplicate-operation rate;
- end-to-end latency;
- cost per verified report.

Use deterministic code evaluators for authorization, citations, invariants,
recovery, and duplicate operations. Use AgentCore Evaluations for session-level
quality dimensions such as completeness, reasoning quality, and report clarity.

## Implementation sequence

### Week 1 — Independent CloudFormation stacks and baseline

- Create the independent CloudFormation stack templates and deployment order.
- Configure IAM, KMS, S3, DynamoDB, and Runtime.
- Create the ECR repository and deployment pipeline.
- Generate the synthetic dataset.
- Deploy read-only Gateway tools.
- Produce the first ACME-482 report.

### Week 2 — S3 Vectors retrieval

- Create the S3 vector bucket and `ENHANCED` index.
- Build the chunking and embedding ingestion pipeline.
- Configure full-text metadata fields.
- Implement hybrid `QueryVectors` retrieval.
- Add freshness, authority, deduplication, and citation reranking.
- Add cross-tenant retrieval tests.

### Week 3 — Policies, identity, and memory

- Add task-aware retrieval policies.
- Configure inbound identity and tenant scope.
- Attach Gateway Policy.
- Create AgentCore Memory namespaces.
- Implement the DynamoDB verified-facts ledger.
- Add deduplication, supersession, provenance, and expiration tests.

### Week 4 — Context optimization

- Implement invariant storage and structured compaction.
- Add token-budget allocation.
- Separate stable and dynamic prompt components.
- Measure prefix reuse and inference token usage.
- Run baseline versus optimized investigations.

### Week 5 — Durable execution

- Add DynamoDB checkpoints and leases.
- Add Step Functions batch orchestration.
- Add schema migration and recovery handling.
- Add operation IDs and uncertain-operation reconciliation.
- Run crash-before-checkpoint and crash-after-operation tests.

### Week 6 — Evaluation and hardening

- Enable AgentCore Observability and CloudWatch Transaction Search.
- Add structured Runtime spans and business metrics for the deployed account.
- Configure AgentCore Evaluations.
- Run the 20-case benchmark.
- Review cost, latency, authorization, and evidence quality.
- Deploy immutable versions and promote only after acceptance tests pass.

## Definition of done

The POC is complete when it can:

1. Investigate ACME-482 through AgentCore Runtime.
2. Identify the idempotency-key regression in deployment 3.8.2.
3. Cite logs, invoices, deployment records, and the retry runbook.
4. Use S3 Vectors hybrid search for exact identifiers and semantic evidence.
5. Exclude another tenant’s relevant-looking document.
6. Ignore prompt-injection text in tool output.
7. Prefer current memory over superseded memory.
8. Preserve all invariants after context compaction.
9. Resume from a DynamoDB checkpoint after simulated failure.
10. Prevent duplicate consequential operations.
11. Produce an auditable AgentCore and CloudWatch trace.
12. Run the same evidence contract across the baseline and expanded synthetic
    incident cases.
13. Return typed, source-linked evidence claims under `ledgerflow.evidence.v2`.

## Official references

- [Amazon Bedrock AgentCore overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
- [AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html)
- [AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-building.html)
- [AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html)
- [AgentCore Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html)
- [AgentCore Memory namespaces](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/specify-long-term-memory-organization.html)
- [AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-evaluator.html)
- [Amazon S3 Vectors](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
- [S3 Vectors hybrid search](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-hybrid-search.html)
- [S3 Vectors metadata filtering](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-metadata-filtering.html)
- [S3 Vectors index configuration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-indexes.html)
- [S3 Vectors with Bedrock Knowledge Bases](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html)
- [S3 Vectors CloudFormation resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_S3Vectors.html)
- [AgentCore CloudFormation resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_BedrockAgentCore.html)

## Implementation progress

### 2026-11-18 — Milestone 1 complete: local vertical slice

- Added the dependency-light local agent workflow in `ledgerflow/agent.py`.
- Added tenant-scoped, read-only business tools in `ledgerflow/tools.py`.
- Added deterministic keyword/vector/RRF/reranking retrieval in
  `ledgerflow/retrieval.py`.
- Added task-aware policies, memory consolidation/expiration, invariant-safe
  context compaction, token allocation, prefix-cache accounting, checkpoints,
  leases, and operation idempotency.
- Added the CLI, Runtime request adapter, deterministic dataset correction, and
  regression tests for the ACME-482 report, cross-tenant exclusion, financial
  write denial, memory supersession, context compaction, and crash recovery.
- Verification: `pytest -q` — 7 tests passed; the local demo produced the
  expected 3.8.2 regenerated-idempotency-key root cause.

### 2026-11-18 — Milestone 2 complete: independent CloudFormation deployment

- Added `cloudformation/deploy.py` with explicit `--plan`, `--change-set`, and
  `--execute` modes. AWS writes are never implicit.
- Added the matching JSON configuration resolver and parameter converter. The
  resolver implements `--set` > `CF_OUT_*` context > process environment > JSON
  `Default`, and fails on unresolved values before a CloudFormation call.
- Added seven independent YAML stacks and seven matching JSON files for
  security/bootstrap, storage, S3 Vectors, Lambda tools, AgentCore, workflow,
  and observability. No stack contains nested-stack resources, exports, or
  `Fn::ImportValue`.
- Added ordinary-output context exchange, package creation, fixture ingestion,
  Lambda tool code, an HTTP Runtime container, and a SigV4 MCP Gateway client.
- Added the S3 Vectors post-create hook that enforces `ENHANCED` mode and
  configures BM25 fields `text` and `title`; the index metadata preserves
  tenant/source/version/authority filters.
- Verification: all seven matching stack contracts pass; the complete
  no-AWS `deploy.py --plan` succeeds; `cfn-lint` passes when its installed
  resource catalog is told to defer the newer AgentCore/S3 Vectors type checks;
  `pytest -q` — 11 tests passed.

### 2026-11-18 — Milestone 3 complete: Runtime and deployment hardening

- Added `ledgerflow/reasoning.py`, an optional Bedrock Converse synthesis stage
  that receives only collected evidence, preserves the base report identity,
  filters citations to the allowed evidence set, removes financial-write
  recommendations, and falls back to deterministic synthesis on failure.
- Added `MODEL_ID` and `ENABLE_MODEL_REASONING` to the AgentCore Runtime stack;
  the default remains `DISABLED` for repeatable local and first-account tests.
- Hardened the SigV4 Gateway client with MCP `initialize`, session-header reuse,
  initialized notification handling, SSE/JSON parsing, and a narrow stateless
  fallback for unsupported handshakes.
- Added adapter tests for MCP session reuse and model-output safety, plus a
  CloudFormation contract test for explicit S3 Vectors index naming and the
  optional model switch.
- Reconciled `PLAN.md`, `TUTORIAL.md`, `README.md`, and `DEPLOYMENT.md` with the
  actual checked-in Runtime implementation; Strands is now documented as a
  compatible replacement seam rather than an undeclared image dependency.
- Verification: `pytest -q` — 14 tests passed; Python compilation, repository
  validation, and the cfn-lint command passed.

### 2026-11-18 — Milestone 4 complete: S3 Vectors readiness and final local verification

- Updated the Stack 02 custom resource to poll `GetIndex` until the configured
  `text` and `title` BM25 fields are `ACTIVE`, or fail before declaring the
  stack ready. Existing fields are checked as well as newly-added fields.
- Verified all seven YAML/JSON contracts individually, the full dependency plan,
  the first-stack no-AWS plan, the vendored Lambda artifact, the local
  ACME-482 report, and crash/resume state hydration.
- Final verification: `pytest -q` — 14 tests passed; Python compilation,
  `cloudformation/validate.py`, and cfn-lint with the documented newer-resource
  catalog exceptions passed. No AWS resources were created.

### 2026-11-18 — Milestone 5 complete: deployment preflight tightening

- Extended the matching-stack validator to fail when a required CloudFormation
  parameter is missing from its JSON configuration, not only when a configured
  parameter is unknown.
- Made `--change-set` explicitly single-stack and documented the review,
  exact-execution, output-hydration sequence required before reviewing the next
  dependent stack.
- Verification: the multi-stack review guard fails safely without importing
  boto3 or calling AWS; all 15 tests, seven stack contracts, Python compilation,
  repository validation, and cfn-lint remain green.

### 2026-11-18 — Milestone 6 complete: embedding model configuration

- Removed the remaining hard-coded ingestion/retrieval model mismatch by
  passing `EmbeddingModelId` through `03-tools.yaml/.json`, Lambda environment
  variables, and the `cloudformation/ingest.py --embedding-model-id` option.
- Kept Amazon Titan Text Embeddings V2 as the default while allowing the model
  to be changed consistently with the configured vector dimension.
- Verification: all 15 tests, seven stack contracts, Python compilation,
  repository validation, and cfn-lint passed after the change.

### 2026-11-18 — Milestone 7 complete: documented ingestion entry point

- Fixed `cloudformation/ingest.py` so both the module form and the documented
  direct script form resolve the repository package correctly.
- Verified the no-upload ingestion dry run seeds the baseline fixture counts
  (17 documents, 5,000 logs, 100 incidents) without making AWS calls; the
  later expanded seed is documented in Milestone 25.
- Final local checks remain green: 15 tests, Python compilation, seven stack
  contracts, repository validation, and cfn-lint.

### 2026-11-18 — Milestone 8 complete: S3 Vectors ARN/name correction

- Corrected Stack 02 to pass the explicit vector-bucket name to the index and
  custom resource. This matters because the S3 Vectors bucket `Ref` is an ARN,
  while SDK calls and the `VectorBucketName` property require the name.
- Kept ARN outputs separate for policies and downstream authorization, and
  added a regression assertion for the name/ARN distinction.
- Verification: 15 tests, seven stack contracts, Python compilation,
  repository validation, and cfn-lint passed.

### 2026-11-18 — Milestone 9 complete: exact change-set regression coverage

- Fixed the exact reviewed change-set execution branch after moving the
  multi-stack review guard ahead of AWS client creation.
- Added a no-AWS test proving the exact change-set path updates the ordinary
  output context without using CloudFormation imports.
- Final verification: 16 tests, seven stack contracts, Python compilation,
  repository validation, and cfn-lint passed.

### 2026-11-18 — Milestone 10 complete: implementation handoff validation

- Verified the local Runtime adapter returns the grounded ACME-482 report.
- Verified simulated crash/resume returns the same incident report after the
  `log_lookup` checkpoint.
- Verified the direct ingestion dry run seeds the then-current baseline of 17
  documents, 100 incidents, and 5,000 application logs without upload. The
  current expanded counts are recorded in Milestone 25.
- Verified the vendored Lambda artifact contains the handler and pinned boto3
  package, and the full deployment plan contains seven independent stacks.
- Final verification: 16 tests passed; Python compilation, required cfn-lint,
  seven stack contracts, no-AWS deployment planning, no-import/no-nested-stack
  guards, local Runtime, recovery, ingestion dry run, and artifact packaging
  all passed. AWS resources remain unapplied by design.

### 2026-11-18 — Milestone 11 complete: AWS security foundation and runtime image

- Applied `ledgerflow-dev-00-security` successfully in the development AWS
  environment, including the ECR repository, artifact bucket, KMS key,
  deployment roles, and runtime execution role.
- The first security-stack attempt exposed an AWS service-principal correction:
  the S3 Vectors KMS grant now uses
  `indexing.s3vectors.amazonaws.com`. The failed stack record was removed and
  the corrected stack was recreated successfully.
- Built the runtime image for `linux/amd64`, pushed it to the created ECR
  repository as `dev-build`, and recorded its immutable digest in the deployment
  handoff before creating the AgentCore Runtime.
- The deployment context now contains the ordinary CloudFormation outputs used
  by later independent stacks; no exports or imports were introduced.

### 2026-11-18 — Milestone 12 complete: Lambda artifact and storage stack

- Vendored the pinned Lambda dependencies, created
  `cloudformation/build/tool_handler.zip`, and uploaded it to the artifact
  bucket at the configured `lambda/tool_handler.zip` key.
- Applied `ledgerflow-dev-01-storage` successfully. The stack created the
  encrypted raw-evidence and fixture DynamoDB storage used by ingestion and
  retrieval, with its ordinary outputs merged into the deployment context.

### 2026-11-18 — Milestone 13 complete: S3 Vectors enhanced index deployed

- Fixed Stack 02 creation ordering by making `EvidenceIndex` explicitly depend
  on `EvidenceVectorBucket`; the bucket name is intentionally passed as a name
  even though the CloudFormation `Ref` value is an ARN.
- Moved the enhanced-index custom resource into the vendored Lambda artifact so
  it uses an SDK that contains the S3 Vectors enhanced-search operations. The
  managed Lambda runtime SDK did not expose those operations.
- Configured the `evidence` index as `ENHANCED` with BM25 text fields `text` and
  `title`, and applied `ledgerflow-dev-02-s3-vectors` successfully.
- AWS reports the new full-text analyzers as `BUILDING`; this is an expected
  asynchronous backfill state. The custom resource now records that state and
  returns after configuration is accepted, while post-deployment verification
  tracks the fields to `ACTIVE` before hybrid-query validation.

### 2026-11-18 — Milestone 14 complete: AWS fixtures and native hybrid query

- Initially uploaded the deterministic baseline fixture set: 17 evidence
  documents, 200 customers, 100 incidents, 200 invoices, 5,000 application
  logs, and 30 deployments. The deployed dataset was later expanded in
  Milestone 25.
- Generated 17 Titan Text Embeddings V2 vectors and wrote them to the enhanced
  `evidence` index with tenant, title, text, authority, status, and source
  metadata.
- Verified both BM25 analyzers reached `ACTIVE` after ingestion.
- Ran an AWS `QueryVectors` smoke test with both query text fields and a vector,
  `queryMode=ENHANCED`, a tenant/public metadata filter, and returned fused
  scores. The result included BM25 scores, vector distance, and native fused
  ranking, confirming the intended hybrid path.

### 2026-11-18 — Milestone 15 complete: read-only tool targets

- Applied `ledgerflow-dev-03-tools` successfully with six read-only Lambda
  targets for incidents, invoices, application logs, deployments, invoice
  state, and evidence search.
- Each target uses the vendored artifact and repeats tenant authorization inside
  the handler; no financial mutation tool is exposed.
- Replaced a YAML anchor/alias shorthand that CloudFormation rejects with
  explicit Lambda environment mappings, then revalidated all seven stack
  contracts before deployment.

### 2026-11-18 — Milestone 16 complete: AgentCore control plane and Runtime

- Applied `ledgerflow-dev-04-agentcore` successfully with AgentCore Memory,
  Policy Engine, Gateway, six Lambda targets, read-only Cedar policy, Runtime,
  and RuntimeEndpoint.
- Corrected the Memory schema by removing the reserved `memory_type` indexed
  key and renamed the runtime event metadata to `memory_kind`.
- Added the Gateway role’s customer-managed KMS permissions required to read
  the encrypted Policy Engine, and configured every Lambda target with the
  required `GATEWAY_IAM_ROLE` credential provider.
- Built and pinned the Runtime as `linux/arm64`, which is the supported
  AgentCore Runtime architecture in this account, and added an explicit
  RuntimeEndpoint dependency so the endpoint waits for Runtime readiness.
- The deployed defaults are `GatewayPolicyMode=ENFORCE`,
  `PolicyEnforcementMode=ACTIVE`, and model reasoning disabled.

### 2026-11-18 — Milestone 17 complete: durable workflow and evaluation

- Applied `ledgerflow-dev-05-workflow` successfully with the Standard Step
  Functions investigation workflow and AgentCore Online Evaluation config.
- Corrected the Step Functions integration to use the data-plane
  `aws-sdk:bedrockagentcore:invokeAgentRuntime` resource and supplied the
  required `AgentRuntimeArn` from Stack 04 ordinary outputs.
- Added the workflow role’s CloudWatch Logs delivery permissions so the state
  machine can create its configured log destination.
- Online evaluation remains `DISABLED` by default; the resource and sampling
  configuration are present for an explicit enablement later.

### 2026-11-18 — Milestone 18 complete: observability and production wiring

- Applied `ledgerflow-dev-06-observability` successfully with the application
  log group, workflow metric filter, alarm, and dashboard.
- Refreshed the Runtime image with the container CA bundle and deployed the
  immutable arm64 image as Runtime version 2. The Runtime and
  `LedgerFlowdevEndpoint` both report `READY`.
- Verified the Gateway MCP transport and corrected the encrypted-data access
  path: the tools, Runtime, and workflow roles now have the KMS permissions
  required for their respective DynamoDB reads/writes and encrypted report
  writes.
- Updated the Runtime error boundary to emit diagnostic tracebacks while still
  returning a JSON error response to the AgentCore HTTP contract.

### 2026-11-18 — Milestone 19 complete: direct Runtime end-to-end test

- Invoked the deployed AgentCore Runtime through its endpoint for ACME-482 /
  `INC-1042` and received HTTP 200.
- The returned report identified the duplicate-invoice root cause, cited the
  expected incident/invoice/log/deployment/runbook evidence IDs, and recorded
  that S3 Vectors hybrid ranking was used. The Runtime remained read-only.
- Verified the final checkpoint is `completed`, the idempotent operation ledger
  entry is `SUCCEEDED`, and the encrypted report object exists under the
  reports bucket with SSE-KMS.

### 2026-11-18 — Milestone 20 complete: Step Functions wrapper test and final QA

- Found and fixed the workflow session-ID boundary: Step Functions now derives
  a compliant AgentCore Runtime session ID with
  `States.Format('ledgerflow-runtime-session-{}-invocation', $.workflow_id)`
  instead of assuming the caller's workflow ID is long enough.
- Started the deployed Standard Step Functions execution and verified it
  reached `SUCCEEDED`, received Runtime status 200, and returned the same
  evidence-grounded report contract.
- Confirmed all seven independent CloudFormation stacks are in a successful
  terminal state, the Runtime is `READY` at version 2, and the enhanced S3
  Vectors index remains `ENHANCED` with both `text` and `title` analyzers
  `ACTIVE`.
- Final local verification passed: 16 tests, Python compilation, required
  cfn-lint, seven stack contracts, no-import/no-nested-stack guards, and
  deployment-context generation.

### 2026-11-18 — Milestone 21 complete: README architecture diagram

- Added a Mermaid architecture diagram to `README.md` covering the independent
  CloudFormation/output-context deployment flow, AgentCore Runtime request
  path, Gateway and read-only Lambda tools, S3 Vectors hybrid retrieval,
  persistence, Step Functions, and observability.

### 2026-11-18 — Milestone 22 complete: bounded LLM investigation logic

- Added `ledgerflow/planner.py`, which uses Bedrock Converse to refine the
  investigation goal, incident query, evidence query, deployment version, and
  read-only evidence categories within a validated schema.
- Enabled Bedrock reasoning in the development Runtime with the verified
  `amazon.nova-micro-v1:0` model. The Runtime now performs model planning and
  evidence-grounded report synthesis while retaining deterministic fallbacks.
- Added encrypted workflow events for checkpoints, Gateway tool calls, model
  planning, model synthesis, result counts, and completion state. Admin-only
  prompt previews are available for audit without exposing private
  chain-of-thought.
- Promoted AgentCore Runtime version 3 to the stable endpoint and verified a
  live HTTP 200 invocation with LLM planning/synthesis events and the same
  evidence-grounded report contract.

### 2026-11-18 — Milestone 23 complete: Lambda-served Cognito console

- Added `ledgerflow-dev-07-web-app`, an independent CloudFormation stack with
  a Lambda-served frontend/API, API Gateway HTTP routes, Cognito User Pool,
  `admins` and `operators` groups, JWT authorization, and optional bootstrap
  admin invitation.
- Added the responsive control-room frontend with Cognito sign-in, admin-only
  user creation/listing, investigation submission, live workflow timeline,
  LLM prompt/plan inspector, tool event stream, and final report view.
- Kept hosting simple: no CloudFront and no public frontend bucket. API Gateway
  serves the UI from Lambda, and the browser never receives AWS credentials.
- Verified the deployed web endpoint returns the UI and dynamic Cognito config;
  unauthenticated API routes return 401. The remaining bootstrap step is to
  deploy Stack 07 with an explicit `BootstrapAdminEmail`.

### 2026-11-18 — Milestone 24 complete: console and model QA

- Confirmed all eight development stacks are in successful terminal states.
- Confirmed AgentCore Runtime version 3 and its stable endpoint are `READY`,
  with `MODEL_REASONING_ENABLED=ENABLED` and
  `amazon.nova-micro-v1:0` configured.
- Confirmed the Lambda-served console returns HTTP 200 for the UI/config assets
  and HTTP 401 for an unauthenticated API route.
- Final repository checks passed: eight stack contracts with cfn-lint, 16
  tests, Python compilation, and JavaScript syntax validation.

### 2026-11-19 — Milestone 25 complete: schema-driven evidence expansion

- Added `ledgerflow/evidence_reasoning.py` with a versioned source schema,
  field aliases for local and AWS records, dynamic evidence-query construction,
  duplicate/retry/timeout/failure analysis, typed claims, and timestamp-aware
  deployment selection.
- Extended `IncidentReport` with `evidence_schema_version` and
  `evidence_claims`; model synthesis now validates claim types, statements, and
  evidence IDs against the authorized packet.
- Added 13 coherent `tenant_acme` cases (`ACME-484` through `ACME-496`,
  `INC-1047` through `INC-1059`) with matching invoices, logs, deployments,
  and four additional runbooks. The expanded seed is 21 documents, 113
  incidents, 213 customers, 231 invoices, 5,150 logs, and 43 deployments.
- Added an Evidence reasoning section to the web console and deployed the
  updated runtime, console artifact, and AWS fixture data.
- Verification: `pytest -q` — 17 tests passed; Python compilation passed; a
  live authenticated `ACME-487` / `INC-1050` workflow completed with
  `ledgerflow.evidence.v2` claims and selected only its prior deployment.

### 2026-11-19 — Milestone 26 complete: operator reasoning and execution log

- Added safe operator-visible `reasoning_plan` and `reasoning_synthesis` event
  summaries. Raw model prompt previews remain admin-only and public summaries
  explicitly mark prompts as redacted.
- Added an Execution log popover with selectable events and structured payload
  details, while preserving tenant authorization on the event API.
- Added the missing runtime-event `TenantId` attribute so Gateway, planning,
  synthesis, and checkpoint events appear in the authorized operator stream.
- Mapped raw tool and checkpoint event names to the user-facing timeline stages,
  and added an explicit `memory_lookup` event so `0 results` is distinguishable
  from a missing event stream.
- Added hover and keyboard-focus timeline details with stage descriptions,
  source/operation labels, event status, result totals, and last-activity time.
- Expanded each timeline detail into a safe per-event trace with timestamps,
  tool targets, result/model facts, deployment versions, evidence counts, and
  report-persistence signals without exposing raw prompts.
- Added concrete safe values for tool inputs, planning queries, source
  selection, latency, report schema/confidence, and evidence IDs.
- Added bounded allow-listed result previews to completed tool and memory
  events, including incident, invoice, log, deployment, and evidence records;
  the console renders these records directly in the timeline detail.
- Verification: 19 tests passed, JavaScript syntax validation passed, and a
  live authenticated operator workflow returned the full 18-event execution
  stream with actual result previews, prompt-redacted reasoning events, a
  memory lookup, and a report.

### 2026-11-19 — Milestone 27 complete: native Bedrock prompt versioning

- Replaced the Runtime's inline planner and synthesis system prompts with two
  native `AWS::Bedrock::Prompt` resources and CloudFormation-managed initial
  `AWS::Bedrock::PromptVersion` snapshots.
- Added `ledgerflow/prompting.py` and a KMS-encrypted
  `ledgerflow-dev-prompt-selections` table. The Runtime resolves the active
  immutable ARN at invocation time, falling back to version 1 when no
  operator selection exists; changing the active version does not rebuild the
  image or restart the endpoint.
- Added the admin-only Prompt versions tab and API routes to list immutable
  versions, create a version from the Bedrock draft, and persist the selected
  active version. Draft editing remains in native Bedrock Prompt Management.
- Added the Bedrock Prompt Management permissions required by the Runtime and
  verified a live Step Functions investigation completed with the active
  immutable planner and synthesis prompt versions. The prior missing
  permission caused a deterministic fallback and was corrected before final
  verification.
- Verification: 20 tests passed, all changed stack contracts passed, the live
  Runtime returned completed planning and synthesis events, and the operator
  API continued to reject prompt management for users outside `admins`.

### 2026-11-19 — Milestone 28 complete: prompt library editor

- Expanded the admin Prompt library tab to load each Bedrock draft's system
  instructions, user template, model, token limit, temperature, and variables.
- Added native `UpdatePrompt` editing for existing runtime prompts and
  `CreatePrompt` for new `ledgerflow-dev-*` CHAT prompts, using the same KMS
  encryption and Bedrock-owned draft/version lifecycle.
- Added prompt-library API routes, API Gateway routes, IAM permissions, and
  editor/create forms. Runtime prompts require the `{{input}}` variable so
  investigations continue to receive their bounded input packet.
- Verified the deployed admin list route returns the planner and synthesis
  draft bodies with their current immutable versions; operators remain denied.

### 2026-11-19 — Milestone 29 complete: one-command deployment

- Added executable `deploy-all.sh` as the repeatable development release path.
- The script runs tests and deployment-contract validation, creates the
  security foundation, packages/uploads Lambda and web artifacts, builds and
  pushes a digest-qualified `linux/arm64` Runtime image, ingests fixtures, and
  deploys all eight independent stacks in dependency order.
- Requires the local deployment Python to provide boto3, with the install
  command documented next to the one-command path; resumable reruns,
  release-specific web artifact keys, and `--skip-ingest`, `--skip-tests`, and
  `--skip-validation` controls keep the script small and explicit.
- Documented the one-command path in `README.md` and `DEPLOYMENT.md` while
  retaining the manual change-set procedure for reviewed deployments.

### 2026-11-19 — Milestone 30 complete: scoped S3 bucket names

- Updated the artifact, raw-data, and report `AWS::S3::Bucket` resources to use
  CloudFormation `BucketNamePrefix` with `BucketNamespace: account-regional`.
- Bucket prefixes remain environment-scoped while AWS derives the account and
  Region suffix, removing the prior dependency on generated physical names and
  the shared global namespace.
- Documented the retained old-bucket migration behavior and added a contract
  regression test for the scoped namespace properties.
