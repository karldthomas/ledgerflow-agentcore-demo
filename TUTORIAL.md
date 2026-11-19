# LedgerFlow AgentCore Tutorial

This tutorial explains the technology choices in `PLAN.md` and how each one
maps to the LedgerFlow incident-response case study.

The target is a production-shaped POC, not a single prompt wrapped around an
LLM. The model performs reasoning, but AWS services and application code own
retrieval, permissions, memory validity, state recovery, and auditing.

## What you will build

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
12 additional `tenant_acme` cases covering unresolved and reconciled timeouts,
payment failures, configuration errors, and duplicate invoices.

## How this tutorial relates to the plan

| Tutorial section | Plan section |
|---|---|
| CloudFormation | CloudFormation deployment plan |
| S3 Vectors | S3 Vectors retrieval design |
| Runtime and reasoning | AgentCore Runtime, bounded Bedrock planning, and report synthesis |
| Gateway and Policy | AgentCore Gateway and Policy |
| Memory | AgentCore Memory |
| Checkpoints | Durable workflow design |
| Tests and metrics | Evaluation plan |

## 1. Understand the service boundaries

### AgentCore Runtime

AgentCore Runtime is the secure hosting environment for the agent application.
It provides sessions, isolation, scaling, endpoints, and runtime observability.
The application owns the bounded investigation flow, evidence contract, and
checkpoint protocol while AWS services provide the managed data and tool
boundaries.

This is different from AgentCore Harness. Harness supplies a managed agent loop;
Runtime supplies the environment in which we run our own loop. The plan chooses
Runtime because explicit context and state behavior are part of the learning
objective.

### LedgerFlow orchestration and bounded model reasoning

The checked-in Runtime image owns the safety and state boundaries. When
`ENABLE_MODEL_REASONING=ENABLED` and `MODEL_ID` are set, the Runtime first
resolves the active native Bedrock Prompt Management version and calls Bedrock
Converse to refine the investigation goal, incident query, evidence query, and
read-only evidence categories. It then calls the selected Gateway targets and
makes a second Converse call through the active synthesis prompt version to
produce the evidence-grounded report. The deployed development stack uses
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
schema. `ledgerflow/reasoning.py` constrains the final response to JSON, keeps
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

### AgentCore Identity

Identity supplies the authenticated principal and workload credentials.

The inbound identity provides values such as:

```text
principal_id
tenant_id
roles
approval_scope
```

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
  validate.py
  ingest.py
  package_artifacts.py
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
AWS::Lambda::Function
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

### Deployment flow

The deployment workflow is:

```bash
python3 cloudformation/validate.py
python3 cloudformation/deploy.py \
  --environment dev \
  --stack 00-security \
  --execute
```

Run the stacks in order, with the artifact build and S3 Vectors ingestion steps
between the infrastructure stacks. In CI, use
`--change-set --stack STACK_ID`, review the returned ID, and apply that exact ID
with `--execute-change-set` before creating the next stack's change set. No
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
```

## 5. Implement the investigation flow

The Runtime application should process an investigation in explicit stages:

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
assemble bounded context
      |
      v
generate structured report
      |
      v
verify citations and constraints
      |
      v
checkpoint and return report
```

The task classifier controls retrieval strategy, not permissions:

| Task | Retrieval behavior |
|---|---|
| Explanation | Runbooks and authoritative documentation |
| Investigation | Logs, invoices, deployments, runbooks, and current memory |
| Financial action | Current invoice state, approval policy, and explicit authorization |

The initial POC implements the financial-action path as read-only and denies
financial writes.

## 6. Implement memory correctly

AgentCore Memory handles short-term session history and long-term semantic
recall. Use tenant-aware namespaces so a memory from one customer cannot be
silently reused for another.

Use DynamoDB as the canonical verified-facts ledger because the case study needs
explicit lifecycle states:

```text
active
superseded
expired
conflict
```

When a new fact arrives:

1. validate its source and authority;
2. check for an identical active fact;
3. merge duplicate provenance;
4. supersede an older fact when the new fact is authoritative and newer;
5. mark equal-authority contradictions as conflicts;
6. exclude expired facts from retrieval;
7. write the memory event and ledger update.

This prevents a superseded retry policy from being treated as current merely
because it is semantically similar to the query.

## 7. Preserve context invariants

The model’s conversation is not the authority for safety rules. Store the
invariants separately:

```json
{
  "no_financial_writes": true,
  "require_evidence_for_findings": true,
  "require_approval_for_remediation": true
}
```

The context builder must reserve space for these values, current task state, and
the final report. Optional history and evidence are selected by relevance and
authority.

When context becomes too large, compact it into a structured summary containing:

- completed steps;
- key findings;
- evidence references;
- unresolved questions;
- failed hypotheses.

After compaction, validate that every invariant and citation remains available.
Retrieved content may contain instructions, but those instructions are data;
they cannot update the invariant store or Gateway Policy.

## 8. Hydrate state after interruption

Use Step Functions to orchestrate batches and DynamoDB to persist business
state. Use the AgentCore session ID as a stable workflow identifier, but do not
depend on the runtime process as the durable source of truth.

Checkpoint after each major read or verified transition. Store:

```text
workflow_id
session_id
customer_id
current_step
completed_steps
checkpoint_version
schema_version
lease_owner
lease_until
pending_operation_id
status
```

On restart:

1. acquire the workflow lease;
2. load and validate the checkpoint;
3. migrate or reject incompatible schema versions;
4. reconcile any pending operation;
5. rebuild the context from durable evidence;
6. resume from the last verified stage.

The deployed Runtime implements this protocol with a versioned DynamoDB
checkpoint and a conditional workflow lease. To verify it deterministically,
enable the opt-in `FailureInjectionEnabled` stack parameter and invoke the
workflow with `failure_after_step` set to `log_lookup`. The first attempt
fails after the durable checkpoint; the Step Functions retry reuses the same
`workflow_id`, loads the saved plan and records, and continues with the next
stage. Disable the parameter after the exercise.

For this POC, all investigation tools are read-only. The operation ledger and
reconciliation tests still model the boundary required before any financial
write is introduced.

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

The first successful report should contain evidence similar to:

```text
INC-1042       duplicate invoice incident
INV-8821       first invoice
INV-8822       duplicate invoice
LOG-8821       retry used a different idempotency key after timeout
DEP-382        deployment changed retry behavior
RUNBOOK-014    retries must reuse the original idempotency key
```

The report should distinguish:

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

To exercise a different branch, submit an investigation for `ACME-488` for a
reconciled timeout or `ACME-485` for a configuration failure through the
deployed console or API. The deployed tenant has the same cases under
`INC-1047` through `INC-1058`.

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
    db.py
    evidence_reasoning.py
    gateway.py
    models.py
    planner.py
    prompting.py
    reasoning.py
    runtime_entry.py
    runtime_server.py
  cloudformation/
    stacks/                  matching YAML/JSON stack pairs
    deploy.py                plan/change-set/execute orchestrator
    validate.py              no-AWS contract validation
    ingest.py                DynamoDB/S3/S3-Vectors fixture loader
    package_artifacts.py     Lambda packaging/upload helper
  lambda/tool_handler.py     read-only Gateway target handlers
  tests/
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
