# LedgerFlow Technical Tutorial

This tutorial explains the technology and implementation of the LedgerFlow
proof of concept. It shows how the repository satisfies the business
requirements in [`CASE_STUDY.md`](CASE_STUDY.md), including the AWS service
boundaries, Python modules, runtime workflow, deployment, recovery behavior,
and verification strategy.

The implementation is a production-shaped POC, not a single prompt wrapped
around an LLM. The model performs bounded reasoning, while AWS services and
application code own retrieval, permissions, memory boundaries, state
recovery, evidence validation, and auditing.

## Implemented system

The final request path looks like this:

```text
Support engineer
      |
      v
AgentCore Runtime
      |
      +--> LedgerFlow orchestrator
      |       +--> optional Bedrock Converse synthesis
      +--> AgentCore Memory
      +--> AgentCore Gateway
              |
              +--> DynamoDB read tools
              +--> S3 Vectors hybrid retrieval
              +--> AgentCore Policy
      |
      +--> DynamoDB workflow checkpoint
      +--> CloudWatch trace and metrics
```

For the canonical test case, the agent must conclude that deployment 3.8.2
caused some retries to use a new idempotency key after a timeout. That
conclusion must be supported by invoice records, application logs, deployment
records, and the idempotent-retry runbook. The same workflow also runs against
the additional `tenant_acme` cases covering unresolved and reconciled
timeouts, payment failures, configuration errors, and duplicate invoices.

## How the documentation is divided

| Document | Purpose |
|---|---|
| [`CASE_STUDY.md`](CASE_STUDY.md) | Business context, use cases, rules, expected outcomes, and acceptance criteria |
| `TUTORIAL.md` | Technology choices, architecture, implementation details, deployment, and verification |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Operational deployment configuration and manual procedures |
| [`README.md`](README.md) | Concise project overview and quick-start commands |

## AgentCore concepts at a glance

AgentCore is a set of managed building blocks, not an autonomous decision
maker. LedgerFlow composes those services with an application-owned workflow,
retrieval policy, evidence contract, and audit layer.

| Concept | What it provides | How LedgerFlow uses it |
| --- | --- | --- |
| Runtime | Hosted execution, sessions, endpoints, and runtime isolation | Runs the Python investigation handler and keeps the model inside a bounded workflow |
| Gateway | A controlled front door for external tools | Exposes the six read-only Lambda tools instead of giving the model direct data-store access |
| Policy | A policy decision layer around tool access | Represents the read-only and tenant-scoped rules; Lambda handlers enforce the same rules independently |
| Identity | User and workload identity | Cognito authenticates operators, while IAM roles authenticate the deployed services without long-lived keys |
| Memory | Managed semantic memory with namespaces and expiry | Retrieves verified-context records for a tenant and subject, and records completed investigation events |
| Observability | Runtime traces and operational telemetry | Combines AgentCore/CloudWatch telemetry with bounded workflow events for the console |
| Evaluations | Session-level quality signals | Provides an optional online-evaluation configuration; deterministic contract tests cover safety and structure |
| Prompt Management | Versioned Bedrock prompts | Stores planner, compaction, and synthesis prompts as drafts and immutable versions |
| Durable workflow | Application-level state and retry control | Step Functions and DynamoDB checkpoint each investigation; this is LedgerFlow orchestration around Runtime |

The important separation is authority. Runtime hosts code, Gateway reaches
tools, Policy expresses access decisions, Memory supplies relevant history, and
the application owns the final decision about which evidence and actions are
allowed.

## Agent engineering best practices demonstrated

The implementation applies these practices beyond the AWS service choices:

| Practice | LedgerFlow implementation |
| --- | --- |
| Bound model autonomy | The model returns a small plan and a structured report; deterministic code chooses the tenant, policy, source scope, and workflow transitions |
| Least privilege | The tool set is read-only and exposes narrow operations rather than arbitrary DynamoDB, S3, or payment-provider access |
| Never use the model as an authority | Authorization, tenant checks, evidence IDs, leases, and publication state are validated outside the model |
| Treat retrieved content as untrusted data | Runbooks, logs, and vector passages can inform a report but cannot change permissions or safety invariants |
| Use structured contracts | Plans, reports, evidence claims, context manifests, checkpoints, and audits have explicit schemas and version fields |
| Separate evidence from memory | Current operational records remain the evidence source; AgentCore Memory enriches the context and is not the source of truth for workflow state |
| Preserve invariants during compaction | Required citations, tenant identity, read-only rules, and approval requirements are retained before optional evidence |
| Make side effects idempotent | Report publication uses a workflow-derived operation ID; future write actions must use the same pattern |
| Validate before publishing | A deterministic Runtime gate checks the report, followed by an independent Step Functions audit over persisted state |
| Degrade safely | Planner, synthesizer, compactor, and memory failures fall back to bounded deterministic behavior or omit optional context |
| Redact sensitive reasoning | Operator events expose lifecycle metadata and safe summaries, not raw prompts or private chain-of-thought |
| Test failure paths | Contract tests cover tenant boundaries, citations, compaction, retries, leases, idempotency, and deployment contracts |
| Keep humans in control of writes | Recommendations are reported, but refunds, credits, cancellations, invoice changes, and other financial writes are not exposed |

The resulting request path is:

```text
authenticated operator
        |
        v
durable workflow -> AgentCore Runtime -> bounded plan
                                      |
                                      v
                              deterministic policy
                                      |
                                      v
                         Gateway -> authorized tools
                                      |
                                      v
                evidence + memory -> bounded context -> model synthesis
                                                        |
                                                        v
                                          deterministic audit -> report
```

## 1. Understand the service boundaries

### AgentCore Runtime

AgentCore Runtime is the secure hosting environment for the agent application.
It provides sessions, isolation, scaling, endpoints, and runtime observability.
The application owns the bounded investigation flow, evidence contract, and
checkpoint protocol while AWS services provide the managed data and tool
boundaries.

This is different from AgentCore Harness. Harness supplies a managed agent loop;
Runtime supplies the environment in which we run our own loop. The implementation
uses Runtime because explicit context and state behavior are part of the system's
safety boundary.

### LedgerFlow orchestration and bounded model reasoning

The checked-in Runtime image owns the safety and state boundaries. When
`ENABLE_MODEL_REASONING=ENABLED` and `MODEL_ID` are set, the Runtime first
resolves the active native Bedrock Prompt Management version and calls Bedrock
Converse to classify the task, refine the investigation goal, incident query,
and evidence query. The application then maps the task to a deterministic
retrieval policy before calling the selected Gateway targets. A second
Converse call through the active synthesis prompt version produces the
evidence-grounded report. The deployed development stack uses
`amazon.nova-micro-v1:0`.

The fixed instructions are no longer assembled in Python. They live in the
planner and synthesis prompts in Bedrock Prompt Management, with an `input`
variable for the bounded JSON packet. `PromptSelectionsTable` stores the
operator-selected immutable version ARN for each stage; an empty row uses the
CloudFormation-created version 1. In the console, administrators use the
Prompt library tab to view and edit native drafts, create new Bedrock prompts,
create immutable versions, and activate a version. The editor writes directly
to the Bedrock draft; it does not create a second prompt store.

`ledgerflow/planner.py` constrains the planning output to a small validated
schema. `ledgerflow/retrieval_policies.py` maps the validated task type to a
bounded source and ranking recipe. `ledgerflow/reasoning.py` constrains the final response to JSON, keeps
only citations that exist in the evidence packet, filters financial-write
recommendations, and falls back to the deterministic report on an invalid or
unavailable model response.

`ledgerflow/evidence_reasoning.py` is the shared schema-driven evidence layer.
It normalizes equivalent fields from AWS records,
builds a retrieval query from incident/log fields, groups invoices by renewal
or subscription, relates retry keys to timeout/failure observations, and
selects the latest deployment at or before the incident. The report contract
uses `evidence_schema_version: "ledgerflow.evidence.v2"` and exposes typed
`evidence_claims` with `fact`, `relationship`, or `hypothesis` types. This
keeps the model's narrative grounded in source records without depending on
fixture-specific IDs.

The model does not own authorization, durable state, or financial actions. The
LedgerFlow-specific code remains responsible for:

- bounded investigation planning;
- read-only tool selection;
- evidence validation;
- schema-driven evidence analysis and typed claim construction;
- bounded model input construction;
- report construction; and
- model and tool trace events for the web console.

The model can refine the investigation, but it cannot select tenants, change
policy, authorize a tool, or write financial state.

### AgentCore Gateway

Gateway is the controlled tool front door. It exposes Lambda functions,
OpenAPI services, or MCP servers as tools discoverable by the agent.

The initial tool set is read-only:

```text
search_incidents
get_customer_invoices
get_application_logs
get_deployments
search_evidence
get_current_invoice_state
```

The agent does not receive direct DynamoDB or S3 permissions for these data
paths. It calls Gateway, Gateway invokes the target, and the target repeats the
tenant check before returning data.

### AgentCore Policy

Policy is the deterministic control layer around Gateway. It is where we
enforce rules such as:

```text
Read-only investigation tools are allowed for an authenticated support user.
Data access must remain within the principal's tenant scope.
Financial mutations are denied in the POC.
An eventual mutation requires a current, matching approval.
```

The policy decision is independent of the model’s classification. If a prompt
injection says that restrictions have been removed, the policy remains
unchanged.

The CloudFormation defaults use `LOG_ONLY` for Gateway and policy
enforcement so a deployment can inspect decisions before enabling blocking
behavior. The Lambda tool handlers still enforce tenant scope and the
read-only contract in application code. After the deny paths have been tested,
the deployment can promote both controls with
`GatewayPolicyMode=ENFORCE` and
`PolicyEnforcementMode=ACTIVE`. Logging is telemetry; it is not a
substitute for enforcement.

### AgentCore Identity

Identity supplies the authenticated principal and workload credentials.

The inbound identity provides values such as:

```text
principal_id
tenant_id
roles
approval_scope
```

In the web path, Cognito claims supply the operator identity, groups, and
tenant context. The web API derives the tenant scope from those claims before
starting a workflow, and the Gateway targets validate it again. This
defense-in-depth approach prevents a caller from changing a tenant ID in a
request body and bypassing the user boundary.

The Runtime workload role provides AWS access without embedding long-lived
access keys in the container.

## 2. Provision everything with CloudFormation

CloudFormation is the infrastructure source of truth for this project. Do not
use CDK or the AgentCore CLI to create production resources.

### Stack structure

Use independent CloudFormation stacks. There is no root stack and no nested
stack resource:

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
  sam_deploy.py
  validate.py
  ingest.py
  requirements.txt
  scripts/
    replace_environment_vars.py
    config_to_parameters.py
```

This POC uses one AWS account and one Region. There are no cross-account roles,
cross-account pipeline actions, Service Catalog products, or cross-account
output lookups.

Each independent stack publishes stable identifiers using ordinary
CloudFormation `Outputs`. The deployment script, rather than CloudFormation
imports, passes those values to the next stack.

```yaml
Outputs:
  VectorIndexName:
    Value: !Ref VectorIndexName
  VectorIndexArn:
    Value: !GetAtt EvidenceIndex.IndexArn
```

The matching JSON file for the consumer stack contains parameter mappings:

```json
{
  "Configuration": [
    {
      "Key": "EnvironmentName",
      "Value": "${EnvironmentName}",
      "Default": "dev"
    },
    {
      "Key": "VectorIndexName",
      "Value": "${CF_OUT_VectorIndexName}"
    },
    {
      "Key": "EnableModelReasoning",
      "Value": "${ENABLE_MODEL_REASONING}",
      "Default": "ENABLED"
    },
    {
      "Key": "ReasoningModelId",
      "Value": "${MODEL_ID}",
      "Default": "amazon.nova-micro-v1:0"
    }
  ]
}
```

The Python deployment code resolves the placeholders, converts the entries to
CloudFormation parameter overrides, deploys the stack, then reads its outputs:

```text
00-security.yaml
  -> describe-stacks
  -> CF_OUT_KmsKeyArn
  -> CF_OUT_ArtifactBucketName

01-storage.json
  ${CF_OUT_KmsKeyArn}

02-s3-vectors.json
  ${CF_OUT_RuntimeExecutionRoleArn}
  ${CF_OUT_ToolsExecutionRoleArn}
```

The resolver uses explicit `--set` overrides first, then the current deployment
context, then process environment variables, then each JSON entry’s `Default`.
This allows the same templates to be deployed to different environments
without editing the YAML.

### Native resource types

The main infrastructure maps to these CloudFormation resources:

```text
AWS::S3::Bucket
AWS::S3Vectors::VectorBucket
AWS::S3Vectors::VectorBucketPolicy
AWS::S3Vectors::Index
AWS::DynamoDB::Table
AWS::ECR::Repository
AWS::Serverless::Function (translated to AWS::Lambda::Function)
AWS::StepFunctions::StateMachine
AWS::BedrockAgentCore::Runtime
AWS::BedrockAgentCore::RuntimeEndpoint
AWS::BedrockAgentCore::Memory
AWS::BedrockAgentCore::Gateway
AWS::BedrockAgentCore::GatewayTarget
AWS::BedrockAgentCore::PolicyEngine
AWS::BedrockAgentCore::Policy
AWS::BedrockAgentCore::OnlineEvaluationConfig
```

S3 Vectors and AgentCore both have native CloudFormation resource types. See
the [S3 Vectors CloudFormation resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_S3Vectors.html)
and [AgentCore CloudFormation resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_BedrockAgentCore.html).

### The image-build boundary

CloudFormation can create the ECR repository and the AgentCore Runtime, but it
does not compile a Docker image. The deployment process therefore has two
parts:

1. CloudFormation creates the ECR repository.
2. CI builds and pushes the Runtime image.
3. CI passes the immutable image digest to CloudFormation.
4. CloudFormation creates or updates the Runtime with that digest.

This is still infrastructure-as-code: the resource, role, endpoint, and image
reference are declared in CloudFormation. The image build is simply an
artifact-production step.

### The SAM deployment boundary

The four stacks that contain Lambda code use the
`AWS::Serverless-2016-10-31` transform. Their templates describe the handler,
runtime, role, timeout, environment, and a repository `CodeUri`; they do not
contain an S3 key for a hand-built ZIP. `cloudformation/sam_deploy.py` resolves
the ordinary stack-output context. It stages only the source needed by those
four stacks, runs `sam build` for every stack, and then runs `sam deploy` for
every stack. Infrastructure-only stacks still go through the SAM build
translation boundary, but produce no Lambda artifact:

```text
Every stack:  SAM build -> template translation
Lambda stack: SAM build -> dependency installation and artifact assembly
Every stack:  SAM deploy -> managed upload (when needed) and CloudFormation change set
```

After Stack 00 creates it, the artifact bucket is passed to SAM as the staging
bucket for later stacks. This keeps the application source, dependency
installation, and CloudFormation resource definition in one repeatable path
while avoiding accidental packaging of the repository's tests, documentation,
or deployment-only data. Stack 00 is small enough to deploy without a staging
bucket; it creates the scoped bucket used by later stacks. `deploy-all.sh`
invokes the SAM adapter for all eight stacks.
`deploy.py --plan` remains the context-aware no-write preview.

### Deployment flow

The deployment workflow is:

```bash
python3 cloudformation/validate.py
python3 cloudformation/sam_deploy.py \
  --environment dev \
  --region "$AWS_REGION" \
  --context-file cloudformation/build/deployment-context-dev.json \
  --stack 00-security
```

Run the stacks in order, with the Runtime image build and S3 Vectors ingestion
steps between the infrastructure stacks. Invoke `sam_deploy.py` for every
stack, passing the Stack 00 artifact bucket after bootstrap. No
`Fn::ImportValue` is required.

## 3. Store the data in the right place

Do not place every record into vector search.

### DynamoDB

Use DynamoDB for exact, current operational state:

- incidents;
- invoices;
- application logs;
- deployments;
- verified memory facts;
- workflow checkpoints;
- operation status.

These records need keyed lookup, conditional writes, expiration, and
concurrency control more than semantic similarity.

### Standard S3

Use a normal S3 bucket for:

- original JSONL fixtures;
- runbooks and deployment documents;
- generated reports;
- evaluation cases;
- exported traces or benchmark results.

### S3 Vectors

Use an S3 vector bucket for the embeddings and searchable document chunks.
S3 Vectors is a specialized vector storage service; it is not the same thing as
a normal S3 bucket.

## 4. Build the S3 Vectors index

### Choose the index configuration

Create an `ENHANCED` vector index with:

- the exact dimension required by the embedding model;
- the model’s recommended distance metric, normally cosine for normalized text
  embeddings;
- filterable metadata for tenant, source type, status, version, authority, and
  effective time;
- full-text search configuration for title and passage text;
- non-filterable metadata for larger passage text that should be returned but
  not used as a filter;
- SSE-KMS encryption and cost-allocation tags.

The dimension, distance metric, and non-filterable metadata choices should be
treated as versioned schema decisions. Create `evidence-v2` rather than making
an incompatible change to a live index.

### Ingest a document

For each chunk:

1. Generate an embedding with a Bedrock embedding model.
2. Give the chunk a stable key such as `RUNBOOK-014#chunk-0`.
3. Store the vector with source metadata and searchable text metadata.
4. Record the document version and checksum.

Conceptually:

```text
runbook text
    |
    +--> chunk text
    +--> embedding vector
    +--> source metadata
    |
    v
S3 Vectors PutVectors
```

### Query the index

For the canonical investigation query:

```text
Why did ACME-482 receive duplicate invoices after deployment 3.8.2?
```

The retrieval service creates:

- the original query text for BM25;
- a query embedding for semantic search;
- a server-derived metadata filter for tenant and document status.

It sends all three to `QueryVectors`.

S3 Vectors then:

1. finds exact lexical matches such as `ACME-482`, `3.8.2`, and
   `PaymentTimeoutException`;
2. finds semantic matches such as the idempotent-retry runbook;
3. fuses the rankings using Reciprocal Rank Fusion;
4. returns one ranked result list.

The application performs a final rerank for authority, freshness, and exact
identifier matches before giving evidence to the agent.

The same retrieval and analysis path can be exercised with the expanded seed:

```text
ACME-484 / INC-1047  unresolved payment timeout
ACME-485 / INC-1048  merchant configuration error
ACME-487 / INC-1050  duplicate invoice after a changed retry key
ACME-488 / INC-1051  reconciled timeout with provider success
ACME-490 / INC-1053  provider payment failure
ACME-495 / INC-1058  timeout followed by a safe retry
ACME-496 / INC-1059  high-volume retry history for context-pressure compaction
```

## 5. Implement the investigation flow

The Runtime application processes an investigation in explicit stages:

```text
request received
      |
      v
authenticate principal
      |
      v
classify task
      |
      v
select retrieval policy
      |
      v
load incident, invoices, logs, deployments
      |
      v
search S3 Vectors for technical evidence
      |
      v
normalize evidence and derive typed claims
      |
      v
load current verified memories
      |
      v
compact context and validate invariants
      |
      v
generate structured report
      |
      v
deterministic pre-publication contract gate
      |
      v
publish report
      |
      v
Step Functions post-run audit -> operator timeline and execution log
```

### Task-aware retrieval policies

The planner now returns a small, validated `task_type`: `investigation`,
`explanation`, or `verification`. The runtime maps that value to the
deterministic registry in `ledgerflow/retrieval_policies.py`. The model can
classify the request and refine the query, but it cannot invent a policy,
choose a tenant, grant permission, or select an arbitrary tool.

The deployed console also exposes a **Task type** selector. **Let the planner
classify** sends no override; choosing a task sends an explicit type through
the API. Both paths end in the same policy registry, so the retrieval recipe
is bounded and observable either way.

| Policy | Sources and strategy | Current limits |
|---|---|---|
| `investigation-v1` | Incident, invoices, logs, deployments, hybrid S3 Vectors, and verified memory | 500 logs, 8 vector results |
| `explanation-v1` | Incident/deployment context plus runbook-only semantic-oriented evidence | 5 vector results, no invoice/log/memory lookup |
| `verification-v1` | Current structured incident, invoice, log, and deployment records plus authoritative runbook corroboration | 250 logs, 8 vector results |

All three policies remain read-only and tenant-scoped. `search_evidence` keeps
the tenant filter in the service, then applies the policy's source-type scope
and ranking strategy. `hybrid` preserves the normal fused ranking,
`semantic` favors conceptual similarity for explanations, and `authoritative`
favors active, high-authority, identifier-matching evidence for verification.

The runtime records a `retrieval_policy` event containing the task type,
policy ID, source list, strategy, and limits. The selected plan and final
report also carry the task type, so an operator can compare the requested task
with the evidence actually collected.

No separate classifier prompt is required. The existing Bedrock planner prompt
was versioned with the new `task_type` field. If the prompt is unavailable or
returns invalid JSON, the runtime uses an explicit UI/API task type when
present, otherwise a small deterministic keyword fallback, and finally the
bounded `investigation-v1` policy. This is a safety fallback, not an attempt
to reproduce model reasoning.

The current implementation remains read-only. Iterative retrieval, explicit
source-freshness rules, retrieval benchmarks, and a full financial-operation
approval path are outside the implemented scope and are tracked as gaps.

## 6. Implement memory correctly

AgentCore Memory is provisioned with a semantic memory strategy and event
expiration. The Runtime retrieves records from a tenant- and subject-scoped
namespace such as `/verified/{tenant_id}/{subject_id}/` and records a
completed investigation report as a memory event.

Memory enriches an investigation; it is not the authoritative source for
customer records or workflow state. A memory read or write failure degrades
gracefully so that primary operational evidence remains available.

The deployment also provisions a DynamoDB verified-facts table and the fixture
generator contains memory records, but the cloud Runtime does not currently
use that table as the source of truth for memory lifecycle. Explicit
deduplication, supersession, conflict handling, and active-versus-expired
fact management remain implementation gaps documented in [`GAPS.md`](GAPS.md).

## 7. Preserve context invariants

Context compaction is not simply asking the model to summarize its previous
messages. It is a controlled transformation of the model's working packet.
The original tool results remain the evidence source; the compacted packet is
only a bounded view used for the next model call.

The model's conversation is not the authority for safety rules. Store the
invariants separately:

```json
{
  "tenant_id": "tenant_acme",
  "read_only": true,
  "no_financial_writes": true,
  "require_evidence_for_findings": true,
  "require_approval_for_remediation": true
}
```

The context builder reserves space for these values, current workflow state,
typed claims, and the final report. Optional evidence and memory are selected
by relevance, authority, and whether the record is already required by a
citation. A source record can be omitted from the prompt while its durable
source ID remains in the checkpoint manifest.

### The implementation boundary

The shared implementation is deliberately independent of AWS services:

```text
ledgerflow/context_compaction.py
  ContextCompactor              deterministic selection and token budget
  CompactedContext               versioned prompt and checkpoint contract
  validate_context               invariant and citation checks
  BedrockContextSummarizer      optional semantic LLM pass
```

`ledgerflow/cloud_agent.py` collects the evidence, creates the compacted
context, and saves a `context_compaction` checkpoint. It then passes that
context to `ledgerflow/reasoning.py`, which sends the bounded JSON packet to
Bedrock. The manifest records the context digest, selected evidence IDs,
full versus citation-only record counts, omitted-record counts, and token
estimate without duplicating the full prompt.

The current implementation uses `ledgerflow.context.v1` and
`ledgerflow.compaction.v1`. The default working-context budget is 8,000
estimated tokens. The estimator is deterministic and dependency-free; the
compactor accepts an exact provider-specific token counter when one is
available.

When context becomes too large, compact it into a structured summary containing:

- completed steps;
- invariant and tenant boundaries;
- typed facts, relationships, and hypotheses;
- selected evidence records and citable source IDs;
- omitted evidence counts and source-ID samples;
- supporting memory records;
- unresolved questions;
- failed hypotheses.

After compaction, `validate_context` checks that every mandatory citation is
still available, every claim cites a selected record, the tenant has not
changed, the read-only invariant remains true, and the token budget is not
exceeded. Retrieved content may contain instructions, but those instructions
are data; they cannot update the invariant store or Gateway Policy.

### Deterministic compaction versus LLM semantic compaction

The deterministic pass decides what is safe to retain. It is responsible for
tenant boundaries, required evidence, citations, token limits, and workflow
state. This makes the safety-critical part repeatable and testable.

When model reasoning is enabled and the native
`ledgerflow-${EnvironmentName}-context-compaction` prompt is configured,
Bedrock performs an optional semantic pass. It can group repeated events,
describe relationships across records, identify candidate hypotheses, and
write unresolved questions. Its output is required to cite IDs already in
`citable_evidence_ids`; it cannot add evidence or change policy. Invalid,
unavailable, or over-budget output falls back to the deterministic context.

The semantic prompt is managed like the planner and synthesis prompts. An
administrator can edit its Bedrock draft, create an immutable version, and
activate the version from the Prompt library tab. The runtime records model
metadata and the context digest, while operator-facing events redact the raw
prompt.

The result is therefore:

```text
durable evidence
      |
      v
deterministic safety envelope
      |
      v
optional LLM semantic summary
      |
      v
deterministic validation and token check
      |
      v
report synthesis
```

This keeps the LLM useful for meaning and compression without making it the
authority for permissions, evidence identity, or durable state.

### Forced compaction exercise

The seed includes a deterministic case that exercises this behavior without
changing a CloudFormation template. `ACME-496` / `INC-1059` has 120 verbose
application-log records and 16 related invoices. The runtime normally collects
up to 500 logs per investigation, so the case supplies enough authorized
evidence to require compaction while remaining tenant-scoped.

Run it from the deployed console with:

```text
Customer ID:       ACME-496
Incident ID:       INC-1059
Tenant:            tenant_acme
Investigation request:
Investigate the high-volume retry history and explain what was retained or reduced.
```

With the default deployment settings (`EVIDENCE_LOG_LIMIT=500`,
`CONTEXT_TOKEN_BUDGET=8000`, and `CONTEXT_COMPACTION_RESERVED_TOKENS=1200`),
the deterministic compactor produces the following reproducible accounting:

```text
input_evidence_count:          138
selected_evidence_count:       138
full_evidence_count:            18
reference_only_evidence_count: 120
omitted_evidence_count:          0
token_count:                 about 6785 / 8000
```

“Full” records contain bounded source fields. “Reference-only” records retain
their source ID and kind but not the verbose payload, so the report can still
cite every selected record without putting every log body into the model
context. The compactor admits mandatory citation IDs first, then upgrades as
many of them as fit, which prevents an early verbose record from crowding out
a later required citation.

To inspect the result, hover over **Compact context** in the timeline. Its
results popover shows input, full, citation-only, omitted, and token counts.
The **Reasoning inspector** shows the safe structured compaction event, and
**Open log** shows the bounded execution payload and evidence-ID samples. Raw
prompt bodies and private chain-of-thought are intentionally not exposed.

The optional Bedrock semantic pass runs after this deterministic selection. It
may group repeated retry observations and add cited relationships or
hypotheses, but invalid or unavailable output falls back to the deterministic
packet. To demonstrate a hard budget failure, lower the token budget until the
mandatory reference IDs themselves cannot fit; the runtime records the
degraded/fallback outcome rather than silently dropping a required citation.

## 8. Hydrate state after interruption

The current Step Functions state machine orchestrates one investigation per
execution. DynamoDB persists its business state. The Runtime uses the workflow
ID as the stable identifier and does not depend on the process as the durable
source of truth.

Checkpoint after each major read or verified transition. Store:

```text
workflow_id
customer_id
tenant_id
current_step
completed_steps
checkpoint_version
state_schema_version
state_data
lease_owner
lease_expires_at
status
failure_injection_consumed
```

On restart:

1. acquire the workflow lease;
2. load and validate the checkpoint;
3. rehydrate state only when the stored schema version is compatible;
4. rebuild the context from durable evidence and the compaction manifest;
5. resume from the last verified stage.

The deployed Runtime implements this protocol with a versioned DynamoDB
checkpoint and a conditional workflow lease. To verify it deterministically,
an administrator opens the **Dev tools** tab in a `dev` or `test` deployment,
arms a one-shot plan for a stable `workflow_id`, and selects `log_lookup` as
the failure checkpoint. The administrator then submits the investigation with
that same ID. The first attempt fails after the durable checkpoint; the Step
Functions retry reuses the ID, loads the saved plan and records, and continues
with the next stage. The Dev tools record becomes **CONSUMED**, and an active
plan can be **DISARMED** before it is claimed.

The implemented failure plan is a small control plane around this checkpoint
protocol:

```text
admin arms plan
      |
      v
ACTIVE control: workflow_id + step + expiry
      |
      v
stage result -> durable checkpoint -> conditional claim
                                      |
                         +------------+------------+
                         |                         |
                    claim succeeds            claim loses/runs out
                         |                         |
                  fail once after save       continue normally
                         |
                         v
                 Step Functions retry
                         |
                         v
              hydrate -> skip -> continue
```

The plan is stored in a DynamoDB table with a workflow/step index and a TTL.
The Runtime only consults it in `dev` and `test`; it claims the record with a
conditional update on `Enabled`, `Consumed`, and expiry. That makes the test
repeatable without putting a failure flag in the investigation request and
prevents two workers from consuming the same plan. A missing control table is
treated as an unavailable optional test hook, so ordinary investigations do not
depend on Dev Tools.

Choose a checkpoint after the work whose retry behavior you want to inspect.
For example, a failure after `log_lookup` leaves the incident, invoice, and
log results in the checkpoint, while the retry starts at deployment lookup. The
timeline and execution log expose the completed checkpoint, the injected
failure, the resumed attempt, and the later tool results. This makes the
recovery test observable rather than merely asserting that a final report was
eventually returned.

For this POC, all investigation tools are read-only. Report publication has an
idempotent operation ledger; uncertain external financial operations and
approval workflows are outside the implemented scope.

## 9. Observe and evaluate the system

Enable AgentCore Observability and CloudWatch Transaction Search. Add custom
spans for:

- task classification;
- selected retrieval policy;
- S3 Vectors query and filters;
- evidence reranking;
- memory reads and writes;
- context compaction;
- checkpoint writes;
- policy decisions;
- recovery attempts.

Build deterministic evaluators for:

- cross-tenant exclusion;
- citation accuracy;
- invariant retention;
- memory freshness;
- duplicate-operation prevention;
- successful crash recovery.

Use AgentCore Evaluations for broader session-level quality such as report
completeness and root-cause explanation quality.

## 10. Test the baseline and expanded cases

A successful report contains evidence similar to:

```text
INC-1042       duplicate invoice incident
INV-8821       first invoice
INV-8822       duplicate invoice
LOG-8821       retry used a different idempotency key after timeout
DEP-382        deployment changed retry behavior
RUNBOOK-014    retries must reuse the original idempotency key
```

A successful report distinguishes:

```text
Confirmed:
- Two invoices share the same subscription renewal.
- The retry used a different idempotency key.
- Deployment 3.8.2 changed the retry handler.
- The runbook requires reuse of the original key.

Recommended but not executed:
- Reconcile affected invoices.
- Restore original-key reuse.
- Add timeout/idempotency regression tests.
```

The agent must not cancel, refund, or modify an invoice.

The report also includes a machine-readable evidence contract:

```json
{
  "evidence_schema_version": "ledgerflow.evidence.v2",
  "evidence_claims": [
    {
      "type": "hypothesis",
      "statement": "A retry with a regenerated idempotency key explains the duplicate renewal.",
      "evidence_ids": ["INV-8821", "INV-8822", "LOG-8821", "DEP-382"]
    }
  ]
}
```

### Deterministic validation inside and outside the Runtime

The deployed workflow uses two complementary deterministic checks. They are
not two different sources of truth and they do not expose private model
reasoning. They validate the structured artifacts that the model and tools
produced:

1. **Pre-publication gate (inside AgentCore Runtime).** After report synthesis,
   the Runtime checks the task policy, tenant scope, evidence schema, required
   citations, typed claim citations, source exclusions, memory boundary, and
   read-only tool contract. The report is published only when this gate passes.
   A failed gate raises a retryable workflow failure before the report is
   marked complete.
2. **Post-run audit (Step Functions).** After the Runtime returns, the state
   machine invokes `WorkflowAuditFunction`. The Lambda rereads the durable
   checkpoint and event records, reconstructs the evidence packet, runs the
   same audit contract in `post_run` mode, and writes a
   `deterministic_audit` event. Step Functions then completes successfully or
   completes with findings based on the audit result.

The distinction is useful for a demo: the first check protects publication at
the point where the report is created, while the second demonstrates that an
independent workflow step can verify the persisted result after the Runtime
has finished. The audit is intentionally a contract check, not a quality
score. It can prove that a citation is authorized and a policy boundary was
kept; it cannot prove that a root-cause hypothesis is true in the real world.

The console surfaces both layers. The timeline has **Validate report** and
**Audit result** stages, the report displays the latest audit summary, and the
Execution log contains the bounded JSON payload with check names, pass/fail
counts, and failed-check identifiers. Click or focus a timeline stage to see
the event trace and returned data. The report is still read-only and no audit
step can authorize a financial write.

To exercise a different branch, submit an investigation for `ACME-488` for a
reconciled timeout or `ACME-485` for a configuration failure through the
deployed console or API. The deployed tenant has the same cases under
`INC-1047` through `INC-1059`.

## Web console

Stack 07 serves the control room directly from Lambda through API Gateway HTTP;
there is no CloudFront distribution or public frontend bucket. Cognito is
configured with `AllowAdminCreateUserOnly`, and the API only permits users in
the `admins` group to create or list users. Operators receive only their
tenant-scoped investigations.

The console calls:

```text
POST /api/investigations
GET  /api/investigations/{workflow_id}
GET  /api/investigations/{workflow_id}/events
GET  /api/admin/users       (admins only)
POST /api/admin/users       (admins only)
GET  /api/admin/prompts     (admins only)
POST /api/admin/prompts/{prompt_key}/versions (admins only)
POST /api/admin/prompts/{prompt_key}/active   (admins only)
```

The workflow view is driven by the encrypted `workflow-events` table. Runtime
events include checkpoints, Gateway tool calls, result counts, LLM planning,
LLM synthesis, and the final report. Every authenticated operator can see the
safe Reasoning inspector, which contains plan decisions, model lifecycle state,
and redaction markers; raw prompt previews remain admin-only. The final report
also renders an “Evidence reasoning” section with each typed claim and its
source IDs. The Execution log has an `Open log` popover where an operator can
select an event and inspect its structured, tenant-scoped payload.

## 11. Recommended repository layout

```text
ledgerflow-agent/
  PLAN.md
  TUTORIAL.md
  DEPLOYMENT.md
  CASE_STUDY.md
  ledgerflow/
    cloud_agent.py
    context_compaction.py
    db.py
    evidence_reasoning.py
    gateway.py
    models.py
    planner.py
    prompting.py
    retrieval_policies.py
    reasoning.py
    runtime_entry.py
    runtime_server.py
  cloudformation/
    deployment.yaml          stack graph and lifecycle-step manifest
    manifest.py              manifest parser and dependency validation
    orchestrate.py           one-command lifecycle runner
    stacks/                  matching YAML/JSON stack pairs
    deploy.py                dependency plan and context resolver
    sam_deploy.py            SAM deploy adapter for all eight stacks
    validate.py              no-AWS contract validation
    ingest.py                DynamoDB/S3/S3-Vectors fixture loader
    requirements.txt         SAM dependency manifest for the custom resource
  lambda/tool_handler.py     read-only Gateway target handlers
  tests/
    test_context_compaction.py
  Dockerfile
  pyproject.toml
  README.md
```

## 12. Official references

- [AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html)
- [AgentCore Runtime versus Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-vs-runtime.html)
- [AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html)
- [AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [AgentCore Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html)
- [AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html)
- [AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-evaluator.html)
- [Bedrock Prompt Management](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-management-create.html)
- [Invoke a managed prompt with Converse](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-management-code-ex.html)
- [S3 Vectors](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
- [S3 Vectors hybrid search](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-hybrid-search.html)
- [S3 Vectors metadata filtering](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-metadata-filtering.html)
- [S3 Vectors full-text configuration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-configuring-full-text-search.html)
- [S3 Vectors CloudFormation resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_S3Vectors.html)
- [AgentCore CloudFormation resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_BedrockAgentCore.html)
