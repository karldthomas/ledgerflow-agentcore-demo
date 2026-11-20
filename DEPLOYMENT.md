# LedgerFlow Deployment Plan

Status: implemented and deployed in the development environment; AWS changes
are explicit and submitted through AWS SAM's CloudFormation change-set flow.

This document defines the deployment process for the LedgerFlow AgentCore POC.
It is intentionally more specific than `PLAN.md`: it defines stack boundaries,
the value-passing protocol, artifact flow, deployment order, validation, and
rollback behavior.

The current development seed contains 21 evidence documents, 113 incidents,
213 customers, 231 invoices, 5,150 application logs, and 43 deployments. The
original ACME-482 walkthrough remains the baseline smoke test; the expanded
tenant_acme cases are `ACME-484` through `ACME-496`. `ACME-496` / `INC-1059`
is a dedicated context-pressure case with 120 verbose logs and 16 related
invoices; it produces citation-only evidence under the default compaction
budget.

The deployed console URL is environment-specific and is printed by the
deployment script after Stack 07 completes; it requires Cognito
authentication.

## 1. Deployment principles

The deployment has these constraints:

- one AWS account;
- one AWS Region;
- independent CloudFormation stacks;
- no nested CloudFormation stacks;
- no AWS Service Catalog;
- no `Export`/`Fn::ImportValue` references;
- no cross-account roles or cross-account pipeline actions;
- no hard-coded resource ARNs in application code;
- CloudFormation is the source of truth for AWS resources;
- a Python deployment orchestrator passes resolved values between stacks;
- stack output values are treated as deployment context, not application
  secrets.

The design follows the useful part of the referenced AWS sample: a template is
paired with a JSON configuration file, Python resolves environment placeholders,
the template is deployed, and its CloudFormation outputs become environment
variables for later deployments.

## 2. Deployment architecture

```text
Developer or CI job
        |
        v
deploy-all.sh
        |
        +--> cloudformation/orchestrate.py
                  |
                  +--> load deployment.yaml
                  +--> run named lifecycle steps
                  +--> cloudformation/sam_deploy.py
                              |
                              +--> derive matching stack JSON
                              +--> resolve ${ENV_VAR} and ${CF_OUT_*}
                              +--> validate parameters
                              +--> sam build (every stack)
                              +--> sam deploy (every stack)
                              +--> wait for SAM's deployment result
                              +--> describe-stacks
                              +--> update deployment context
        |
        v
Independent CloudFormation stacks
        |
        +--> 00-security
        +--> 01-storage
        +--> 02-s3-vectors
        +--> 03-tools
        +--> 04-agentcore
        +--> 05-workflow
        +--> 06-observability
        +--> 07-web-app
```

The stacks are independent CloudFormation stacks. The dependency relationship
is implemented by the context resolver used by `sam_deploy.py`, not by
CloudFormation intrinsic references. `deploy.py --plan` remains the no-write
dependency preview; `sam_deploy.py` is the write path for every stack.

### 2.1 Deployment manifest and lifecycle steps

The reusable deployment contract is a manifest such as
`cloudformation/deployment.yaml`. A shared Python loader
(`cloudformation/manifest.py`) parses it for both `deploy.py --plan` and
`sam_deploy.py`; `cloudformation/orchestrate.py` executes the named lifecycle
steps, and `deploy-all.sh` is only a shell entry point. No shell code parses
YAML or duplicates stack order.

The manifest owns stack identity, dependency order, naming, and named lifecycle
steps:

```yaml
version: 1
name: ledgerflow
stack_name_pattern: "{name}-{environment}-{id}"

stacks:
  - id: 00-security
    template: stacks/00-security.yaml
    depends_on: []
  - id: 01-storage
    template: stacks/01-storage.yaml
    depends_on: [00-security]

steps:
  - id: build-runtime-image
    handler: build_runtime_image
    after: [00-security]
    before: [01-storage]
  - id: ingest-fixtures
    handler: ingest_fixtures
    after: [02-s3-vectors]
    before: [03-tools]
```

The JSON configuration path is never repeated in the manifest. The loader
derives it from the template path with the same basename:

```text
stacks/01-storage.yaml -> stacks/01-storage.json
```

The loader validates the manifest version, unique stack IDs, template/config
pairs, dependency references, cycles, and lifecycle-step placement before any
AWS call. The orchestrator validates each handler against its allow-list before
running it. The loader returns a stable topological order and the transitive
predecessors needed to hydrate `CF_OUT_*` values when a deployment resumes.

Lifecycle handlers are an allow-listed Python registry, not arbitrary shell
commands from the manifest. The initial handlers cover the local test suite,
contract validation, AWS identity verification, Runtime image build/push, and
deterministic fixture ingestion. This keeps the deployment reusable without
allowing a data file to execute unreviewed commands.

`sam build` still runs for every stack. For a stack containing
`AWS::Serverless::Function`, the SAM template and its resource metadata control
the Lambda build. For an infrastructure-only stack, the build produces the
translated deployment template but no Lambda artifact. `sam deploy` then owns
the CloudFormation change set and progress reporting for both kinds of stack.

## 3. Repository layout

```text
cloudformation/
  deployment.yaml
  manifest.py
  orchestrate.py
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
    validate_stack_contract.py
  build/
    .gitkeep
```

The YAML and JSON files intentionally share a basename. For example:

```text
stacks/02-s3-vectors.yaml
stacks/02-s3-vectors.json
```

`deploy.py --stack 02-s3-vectors --plan` locates both files automatically from
the manifest.
`sam_deploy.py` is the single deployment adapter: it stages only the required
Lambda source, runs `sam build` for every stack, and then submits every stack
with `sam deploy`. No manually created Lambda ZIP is required.

Run the repository-only deployment validation with:

```bash
python3 cloudformation/validate.py
```

This checks the output dependency graph and forbidden coupling, then runs
`cfn-lint` with only the newer AgentCore/S3 Vectors resource-catalog checks
deferred when the installed catalog does not know those resource types.

## 4. Value exchange protocol

### 4.1 Producer stack outputs

Producer stacks publish ordinary CloudFormation outputs:

```yaml
Outputs:
  KmsKeyArn:
    Description: Customer-managed KMS key used by LedgerFlow resources
    Value: !GetAtt LedgerFlowKey.Arn

  ArtifactBucketName:
    Description: SAM staging bucket and deployment artifacts
    Value: !Ref ArtifactBucket
```

These values are visible through `describe-stacks` after deployment. They are
not exported and are not imported by another template.

### 4.2 Deployment context names

`deploy.py` maps each output to an environment-style name:

```text
Output key:       KmsKeyArn
Context variable: CF_OUT_KmsKeyArn

Output key:       ArtifactBucketName
Context variable: CF_OUT_ArtifactBucketName
```

The context is held in memory during a deployment run and also written to a
non-secret diagnostic file:

```text
cloudformation/build/deployment-context-dev.json
```

The context file is useful for debugging and resuming a deployment. It must not
contain secrets, tokens, passwords, or private credentials.

### 4.3 Consumer stack configuration

The consumer’s matching JSON file refers to the context variable:

```json
{
  "Configuration": [
    {
      "Key": "EnvironmentName",
      "Value": "${EnvironmentName}",
      "Default": "dev"
    },
    {
      "Key": "KmsKeyArn",
      "Value": "${CF_OUT_KmsKeyArn}"
    },
    {
      "Key": "ArtifactBucketName",
      "Value": "${CF_OUT_ArtifactBucketName}"
    }
  ]
}
```

The corresponding template declares normal parameters:

```yaml
Parameters:
  EnvironmentName:
    Type: String

  KmsKeyArn:
    Type: String

  ArtifactBucketName:
    Type: String
```

The template does not know which stack produced those values. It only receives
resolved parameters during deployment.

### 4.4 Resolution precedence

For a placeholder such as `${NAME}`, the resolver uses this order:

1. an explicit `--set NAME=value` command-line override;
2. a value already in the deployment context, including `CF_OUT_*` values;
3. a process environment variable;
4. the JSON entry’s `Default` value;
5. a deployment error if the value is still missing.

Unresolved placeholders must fail before CloudFormation is called.

## 5. Context-aware deployment adapter

`sam_deploy.py` owns stack writes and deployment feedback. It uses the
resolution, validation, hydration, and output-context helpers in `deploy.py`; it
does not contain business logic or create AWS resources directly outside the
SAM/CloudFormation deployment flow. `deploy.py --plan` is the review-only
dependency and parameter preview.

### 5.1 Inputs

```text
deploy.py --plan:

--environment dev
--region us-east-1
--stack 00-security              optional single-stack preview
--from 00-security               start from a stack and continue
--plan                           resolve and validate without deploying
--set KEY=VALUE                  explicit parameter override

sam_deploy.py:

--environment dev
--region us-east-1
--stack STACK_ID                 deploy one stack through SAM
--s3-bucket BUCKET               SAM staging bucket after Stack 00 exists
--resolve-s3                     let SAM create a managed staging bucket
--set KEY=VALUE                  explicit parameter override
```

Environment variables used by the deployment include:

```text
AWS_REGION
ENVIRONMENT_NAME
RUNTIME_IMAGE_URI
EMBEDDING_MODEL_ID
MODEL_ID
```

### 5.2 Deployment algorithm

The manifest-driven runner executes the following sequence:

1. load and validate the manifest, including derived `<template>.json` paths;
2. run lifecycle steps positioned before the first stack;
3. calculate the CloudFormation stack name;
4. load `<stack>.yaml` and its derived `<stack>.json`;
5. resolve environment and `CF_OUT_*` placeholders;
6. validate that every resolved parameter exists in the template;
7. validate that required template parameters have values;
8. validate the contract; run repository-wide `cfn-lint` before the deployment;
9. stage Lambda source when needed and run `sam build` for every stack;
10. invoke `sam deploy`, which creates and executes the CloudFormation change
   set and reports deployment progress;
11. run lifecycle steps positioned after that stack;
12. fail immediately if the stack reaches a rollback or failure state;
13. call `describe-stacks` after a successful execution;
14. map outputs to `CF_OUT_*` context values;
15. persist the non-secret deployment context;
16. continue to the next stack.

For `sam_deploy.py --stack`, the adapter hydrates predecessor outputs directly
from `describe-stacks`. For `deploy.py --from`, the same manifest order is used
for a no-write dependency preview. The local context file is a cache and
recovery aid, not a CloudFormation import mechanism.

### 5.3 Stack naming

Use deterministic names:

```text
ledgerflow-dev-00-security
ledgerflow-dev-01-storage
ledgerflow-dev-02-s3-vectors
ledgerflow-dev-03-tools
ledgerflow-dev-04-agentcore
ledgerflow-dev-05-workflow
ledgerflow-dev-06-observability
ledgerflow-dev-07-web-app
```

Stack names must be stable across updates so CloudFormation performs updates
instead of creating replacement stacks.

### 5.4 Idempotency

The deployment must be safe to run repeatedly:

- use deterministic stack names;
- use SAM's managed change-set/update behavior;
- do not create random physical names unless replacement is intended;
- use immutable ECR image digests;
- use stable S3 Vectors bucket and index names;
- preserve stateful resources with deletion policies;
- treat an empty change set as success.

## 6. Stack inventory and order

### Stack 00 — Security and bootstrap

Template: `00-security.yaml`

Creates:

- customer-managed KMS key and alias;
- standard S3 artifact bucket;
- ECR repository for the AgentCore Runtime image;
- base Runtime execution role;
- Gateway execution role;
- Lambda tool execution role;
- Step Functions execution role;
- AgentCore evaluation execution role.

Inputs:

```text
EnvironmentName
RetentionDays
AgentRepositoryName
```

Outputs:

```text
KmsKeyArn
ArtifactBucketName
RuntimeRepositoryUri
RuntimeExecutionRoleArn
ToolsExecutionRoleArn
GatewayExecutionRoleArn
WorkflowExecutionRoleArn
EvaluationExecutionRoleArn
```

The artifact bucket uses the S3 account-regional namespace with the prefix
`ledgerflow-${EnvironmentName}-artifacts`. CloudFormation appends the owning
account, Region, and `-an` suffix; the resolved bucket name remains available
as `CF_OUT_ArtifactBucketName`.

The Gateway role includes permission to invoke only the environment-prefixed
LedgerFlow Lambda functions. Later stacks consume these role ARNs through the
ordinary output context.

### Artifact build step

After Stack 00 succeeds:

1. build the Runtime container;
2. authenticate Docker to the ECR repository;
3. push the image;
4. resolve the immutable image digest;
5. set `RUNTIME_IMAGE_URI` to the digest-qualified URI;
6. let `sam_deploy.py` submit the remaining stacks through `sam deploy`; SAM
   builds every template and uploads Lambda artifacts to the Stack 00 artifact
   bucket when a function-bearing stack is reached.

CloudFormation creates the ECR repository. CI or the local deployment process
creates the image artifact. This is the unavoidable artifact boundary; the
Runtime resource still receives its image URI through CloudFormation.

### Stack 01 — Operational storage

Template: `01-storage.yaml`

Creates:

- raw-data S3 bucket;
- report S3 bucket;
- incidents table;
- invoices table;
- application logs table;
- deployments table;
- verified-facts table;
- workflow-checkpoints table;
- prompt-selections table for the operator-selected Bedrock prompt version;
- Dev Tools controls table for admin-only, one-shot recovery plans;
- operation-ledger table;
- appropriate TTL configuration and point-in-time recovery.

The raw-data and report buckets use the same account-regional namespace with
the prefixes `ledgerflow-${EnvironmentName}-raw` and
`ledgerflow-${EnvironmentName}-reports`. This avoids the shared global S3
namespace while keeping the physical names predictable within the owning
account and Region. The S3 Vectors bucket is a separate vector-bucket resource
and is already unique per account and Region.

The existing development buckets were created before this naming change and
have `Retain` replacement policies. Updating Stack 00/01 will create the new
scoped buckets and retain the old generated-name buckets; copy any data that
must be preserved before retiring the old buckets.

Inputs:

```text
EnvironmentName
KmsKeyArn = ${CF_OUT_KmsKeyArn}
RetentionDays = ${RETENTION_DAYS}
```

Outputs:

```text
CustomersTableName
RawDataBucketName
ReportsBucketName
IncidentsTableName
InvoicesTableName
ApplicationLogsTableName
DeploymentsTableName
VerifiedFactsTableName
WorkflowCheckpointsTableName
OperationLedgerTableName
PromptSelectionsTableName
DevToolsControlsTableName
```

### Stack 02 — S3 Vectors

Template: `02-s3-vectors.yaml`

Creates:

- S3 vector bucket;
- vector bucket policy;
- `ENHANCED` vector index;
- SSE-KMS configuration;
- vector-index tags;
- a Lambda-backed custom resource that sets the index mode to `ENHANCED` and
  adds the BM25 full-text fields.

Inputs:

```text
EnvironmentName
KmsKeyArn = ${CF_OUT_KmsKeyArn}
RuntimeExecutionRoleArn = ${CF_OUT_RuntimeExecutionRoleArn}
ToolsExecutionRoleArn = ${CF_OUT_ToolsExecutionRoleArn}
VectorIndexName = ${VECTOR_INDEX_NAME}
VectorDimension = ${VECTOR_DIMENSION}
EmbeddingModelId = ${EMBEDDING_MODEL_ID} (default amazon.titan-embed-text-v2:0)
TextSearchFields = ${VECTOR_TEXT_SEARCH_FIELDS}
```

Outputs:

```text
VectorBucketArn
VectorBucketName
VectorIndexArn
VectorIndexName
VectorIndexMode
FullTextSearchFields
```

The index name is passed explicitly because `Ref` on
`AWS::S3Vectors::Index` returns the index ARN, while SDK calls need the name.
The dimension, searchable text fields, and non-filterable metadata fields are
schema decisions. The vector bucket must be empty before it can be deleted, so
use a retain policy for development and production data unless an explicit
cleanup process is available.

### Post-stack 02 ingestion

After Stack 02 succeeds:

1. seed the deterministic fixture database;
2. write the raw document fixture to the raw-data bucket;
3. create Titan Text Embeddings V2 embeddings at the configured dimension;
4. write vectors with `PutVectors`;
5. attach tenant, source type, version, authority, timestamp, and checksum
   metadata;
6. verify the custom resource’s full-text fields reach `ACTIVE`;
7. run exact, semantic, and hybrid retrieval smoke tests.

Data ingestion is not represented as a CloudFormation resource. It is a
repeatable post-deployment job driven by the resolved `VectorBucketName` and
`VectorIndexName` outputs.

### Stack 03 — Tools and retrieval services

Template: `03-tools.yaml`

Creates:

- Lambda for incident lookup;
- Lambda for invoice lookup;
- Lambda for application-log lookup;
- Lambda for deployment lookup;
- Lambda for current invoice-state lookup;
- Lambda for S3 Vectors hybrid retrieval;
- AgentCore invocation permissions.

Inputs:

```text
ToolsExecutionRoleArn = ${CF_OUT_ToolsExecutionRoleArn}
IncidentsTableName = ${CF_OUT_IncidentsTableName}
InvoicesTableName = ${CF_OUT_InvoicesTableName}
ApplicationLogsTableName = ${CF_OUT_ApplicationLogsTableName}
DeploymentsTableName = ${CF_OUT_DeploymentsTableName}
CustomersTableName = ${CF_OUT_CustomersTableName}
VectorBucketName = ${CF_OUT_VectorBucketName}
VectorIndexName = ${CF_OUT_VectorIndexName}
VectorDimension = ${VECTOR_DIMENSION}
```

Outputs:

```text
SearchIncidentsFunctionArn
GetCustomerInvoicesFunctionArn
GetApplicationLogsFunctionArn
GetDeploymentsFunctionArn
GetCurrentInvoiceStateFunctionArn
SearchEvidenceFunctionArn
```

All initial tools are read-only. Do not create or deploy financial mutation
functions in this stack.

### Stack 04 — AgentCore

Template: `04-agentcore.yaml`

Creates:

- AgentCore Memory;
- AgentCore policy engine;
- read-only Gateway;
- Gateway targets for the tool Lambdas;
- AgentCore Runtime;
- Runtime endpoint;
- native Bedrock Prompt Management resources for planning, context compaction,
  and synthesis;
- initial immutable prompt versions created from those resources;
- read-only Cedar Policy;
- Runtime environment variables containing resolved resource names, prompt ARNs,
  and default immutable version ARNs.

Inputs:

```text
RuntimeImageUri
ReasoningModelId = ${MODEL_ID}                 optional
EnableModelReasoning = ${ENABLE_MODEL_REASONING} (default DISABLED)
ContextCompactionLlmEnabled = ${CONTEXT_COMPACTION_LLM_ENABLED} (default ENABLED)
ContextTokenBudget = ${CONTEXT_TOKEN_BUDGET} (default 8000)
ContextCompactionReservedTokens = ${CONTEXT_COMPACTION_RESERVED_TOKENS} (default 1200)
EvidenceLogLimit = ${EVIDENCE_LOG_LIMIT} (default 500; maximum 500)
WorkflowLeaseSeconds = ${WORKFLOW_LEASE_SECONDS} (default 300; Runtime renews the lease around long-running stages)
FailureInjectionEnabled = ${FAILURE_INJECTION_ENABLED} (default DISABLED; legacy direct-payload compatibility guard)
RuntimeExecutionRoleArn = ${CF_OUT_RuntimeExecutionRoleArn}
GatewayExecutionRoleArn = ${CF_OUT_GatewayExecutionRoleArn}
GatewayPolicyMode = ${GATEWAY_POLICY_MODE}
PolicyEnforcementMode = ${POLICY_ENFORCEMENT_MODE}
VectorBucketName = ${CF_OUT_VectorBucketName}
VectorIndexName = ${CF_OUT_VectorIndexName}
RawDataBucketName = ${CF_OUT_RawDataBucketName}
ReportsBucketName = ${CF_OUT_ReportsBucketName}
WorkflowCheckpointsTableName = ${CF_OUT_WorkflowCheckpointsTableName}
OperationLedgerTableName = ${CF_OUT_OperationLedgerTableName}
PromptSelectionsTableName = ${CF_OUT_PromptSelectionsTableName}
DevToolsControlsTableName = ${CF_OUT_DevToolsControlsTableName}
SearchIncidentsFunctionArn = ${CF_OUT_SearchIncidentsFunctionArn}
GetCustomerInvoicesFunctionArn = ${CF_OUT_GetCustomerInvoicesFunctionArn}
GetApplicationLogsFunctionArn = ${CF_OUT_GetApplicationLogsFunctionArn}
GetDeploymentsFunctionArn = ${CF_OUT_GetDeploymentsFunctionArn}
GetCurrentInvoiceStateFunctionArn = ${CF_OUT_GetCurrentInvoiceStateFunctionArn}
SearchEvidenceFunctionArn = ${CF_OUT_SearchEvidenceFunctionArn}
MemoryEventExpiryDays = ${MEMORY_EVENT_EXPIRY_DAYS}
```

Outputs:

```text
MemoryId
PolicyEngineArn
PolicyEngineId
GatewayId
GatewayArn
GatewayUrl
RuntimeArn
RuntimeId
RuntimeEndpointArn
RuntimeEndpointId
PlannerPromptArn
PlannerPromptVersionArn
SynthesisPromptArn
SynthesisPromptVersionArn
```

The JSON configuration defaults the Gateway to `ENFORCE` and the Policy to
`ACTIVE`; the Runtime model-synthesis switch remains `DISABLED`. A controlled
rollout may explicitly select both `LOG_ONLY` values, and should return to
`--set GatewayPolicyMode=ENFORCE --set PolicyEnforcementMode=ACTIVE` after
authorization tests pass.

The Runtime calls Bedrock Converse with the selected prompt-version ARN as
`modelId` and supplies only the `input` prompt variable. The fixed instructions,
chat message template, and inference limits are stored in Bedrock Prompt
Management; the DynamoDB selection table stores only the active version ARN.

### Stack 05 — Workflow and evaluations

Template: `05-workflow.yaml`

Creates:

- Step Functions state machine for batch investigations;
- CloudWatch Logs for workflow execution;
- AgentCore online evaluation configuration, disabled by default.

Inputs:

```text
RuntimeId = ${CF_OUT_RuntimeId}
WorkflowExecutionRoleArn = ${CF_OUT_WorkflowExecutionRoleArn}
EvaluationExecutionRoleArn = ${CF_OUT_EvaluationExecutionRoleArn}
EvaluationSamplingPercentage = ${EVALUATION_SAMPLING_PERCENTAGE}
EnableOnlineEvaluation = ${ENABLE_ONLINE_EVALUATION}
```

Outputs:

```text
StateMachineArn
StateMachineName
EvaluationConfigArn
WorkflowLogGroupName
```

### Stack 06 — Observability

Template: `06-observability.yaml`

Creates:

- a dedicated application log group;
- a metric filter and alarm for failed Step Functions investigations;
- a CloudWatch dashboard linking the Runtime endpoint, evaluation config, and
  state machine.

Inputs:

```text
EnvironmentName
StateMachineArn = ${CF_OUT_StateMachineArn}
WorkflowLogGroupName = ${CF_OUT_WorkflowLogGroupName}
RuntimeEndpointArn = ${CF_OUT_RuntimeEndpointArn}
EvaluationConfigArn = ${CF_OUT_EvaluationConfigArn}
LogRetentionDays = ${LOG_RETENTION_DAYS}
```

Outputs:

```text
ApplicationLogGroupName
InvestigationFailureAlarmArn
DashboardName
```

### Stack 07 — Web console and Cognito access

Template: `07-web-app.yaml`

Creates:

- a Lambda-served HTML/CSS/JavaScript control room behind API Gateway HTTP;
- a Cognito User Pool configured for admin-created users only;
- `admins` and `operators` Cognito groups;
- JWT-protected investigation, admin-user, and prompt-version API routes;
- a workflow-events table reader for the visual timeline and LLM inspector;
- an optional CloudFormation bootstrap admin invite when
  `BootstrapAdminEmail` is supplied.

The browser never receives AWS credentials. Only users in the Cognito
`admins` group can call the user-management routes. The Lambda API derives or
checks tenant scope from JWT claims before returning checkpoints, events, or
reports. Prompt version listing, creation, and activation are also admin-only.

Inputs:

```text
KmsKeyArn = ${CF_OUT_KmsKeyArn}
StateMachineArn = ${CF_OUT_StateMachineArn}
WorkflowCheckpointsTableName = ${CF_OUT_WorkflowCheckpointsTableName}
WorkflowEventsTableName = ${CF_OUT_WorkflowEventsTableName}
ReportsBucketName = ${CF_OUT_ReportsBucketName}
PromptSelectionsTableName = ${CF_OUT_PromptSelectionsTableName}
DevToolsControlsTableName = ${CF_OUT_DevToolsControlsTableName}
PlannerPromptArn = ${CF_OUT_PlannerPromptArn}
SynthesisPromptArn = ${CF_OUT_SynthesisPromptArn}
PlannerPromptDefaultVersionArn = ${CF_OUT_PlannerPromptVersionArn}
SynthesisPromptDefaultVersionArn = ${CF_OUT_SynthesisPromptVersionArn}
ContextCompactionPromptArn = ${CF_OUT_ContextCompactionPromptArn}
ContextCompactionPromptDefaultVersionArn = ${CF_OUT_ContextCompactionPromptVersionArn}
BootstrapAdminEmail = ${BOOTSTRAP_ADMIN_EMAIL} (optional)
```

Outputs:

```text
WebApiUrl
UserPoolId
UserPoolClientId
UserPoolArn
```

## 7. Configuration file contract

Every stack JSON file uses this structure:

```json
{
  "Configuration": [
    {
      "Key": "EnvironmentName",
      "Value": "${EnvironmentName}",
      "Default": "dev",
      "CreateParameter": true
    },
    {
      "Key": "KmsKeyArn",
      "Value": "${CF_OUT_KmsKeyArn}",
      "CreateParameter": true
    },
    {
      "Key": "OptionalTag",
      "Value": "ledgerflow",
      "CreateParameter": false
    }
  ]
}
```

Rules:

- `Key` must match a CloudFormation parameter or an explicitly supported tag.
- `Value` can contain `${...}` placeholders.
- `Default` is required for values that may be omitted from the environment.
- `CreateParameter: false` prevents a configuration entry from becoming a
  CloudFormation parameter override.
- tags and parameters must be handled separately;
- values that resolve to secrets are prohibited;
- unknown keys fail validation rather than being silently ignored.

## 8. Preflight checks

Before deploying:

1. verify AWS identity with `sts get-caller-identity`;
2. verify the selected account and Region match the deployment configuration;
3. verify AgentCore and S3 Vectors resource types are available in the Region;
4. verify Bedrock model access for the selected reasoning and embedding models;
5. verify the deploying principal or CI role has the required CloudFormation,
   IAM, S3, ECR, Lambda, AgentCore, DynamoDB, and S3 Vectors permissions;
6. verify the local Docker toolchain for the Runtime image;
7. run Python unit tests;
8. run `python3 cloudformation/validate.py`;
9. validate every YAML/JSON basename pair and output dependency;
10. run `deploy.py --environment dev --stack 00-security --plan`;
11. use the full `deploy.py --environment dev --plan` as a dependency preview;
    later-stack `CF_OUT_*` values remain symbolic until predecessor stacks run;
12. verify that no configuration file contains credentials or secret values.

## 9. First deployment procedure

### One-command development deployment

For the normal development path, the repository now provides a single
orchestrator that runs the repository tests and CloudFormation validation,
submits Stack 00 through `sam deploy`, builds and pushes the ARM64 Runtime
image, ingests the deterministic fixtures, and submits Stacks 01–07 through
the same `sam deploy` path in dependency order:

```bash
python3 -m pip install --user boto3
./deploy-all.sh
```

The deployment host must also have the AWS SAM CLI and Docker installed. The
script uses ambient AWS environment variables and does not select an AWS
profile. Stack 00 is small enough for SAM to send directly to CloudFormation;
once it creates the scoped artifact bucket, later stacks pass that bucket to
SAM for template and Lambda artifact uploads.

Before running it, provide standard ambient AWS credentials such as
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`, and
`AWS_REGION`.

Set `BOOTSTRAP_ADMIN_EMAIL` on the first deployment if the Cognito bootstrap
custom resource should create or invite the initial administrator:

```bash
BOOTSTRAP_ADMIN_EMAIL=admin@example.com \
./deploy-all.sh
```

The script records a digest-qualified Runtime image. For Stacks 02, 03, 05,
and 07, it stages the required source files, runs `sam build`, and uses
`sam deploy` with the Stack 00 artifact bucket as SAM's staging bucket. No
Lambda ZIP is manually created or uploaded. If a run stops after a successful
stack, rerun the same command; the stable stack names and deployment context
make the operation resumable. `--skip-ingest` is available when the fixture
data is already loaded. `--skip-tests` and `--skip-validation` are available
for controlled follow-up deployments.

This path executes CloudFormation changes immediately through SAM's managed
change-set flow for all eight stacks. Use the manual procedure below when a
single stack should be deployed independently.

### Step 1 — Security/bootstrap

```bash
python3 cloudformation/sam_deploy.py \
  --environment dev \
  --region "$AWS_REGION" \
  --context-file cloudformation/build/deployment-context-dev.json \
  --stack 00-security
```

This uses `sam deploy` directly against the small bootstrap template; no
staging bucket exists yet. Confirm that the deployment context contains the
KMS, artifact bucket, ECR repository, and role outputs before continuing.

### Step 2 — Build and publish the Runtime image and SAM artifacts

Build and push the Runtime image to the ECR URI from
`CF_OUT_RuntimeRepositoryUri`. The all-in-one script also uses the Stack 00
artifact bucket as SAM's staging bucket; SAM performs Lambda dependency
installation, build, packaging, and upload.

Set:

```text
RUNTIME_IMAGE_URI=<digest-qualified ECR URI>
```

For an independent Lambda-bearing stack deployment, use the SAM adapter after
its predecessor outputs are present in the deployment context:

```bash
python3 cloudformation/sam_deploy.py \
  --environment dev \
  --region "$AWS_REGION" \
  --context-file cloudformation/build/deployment-context-dev.json \
  --stack 03-tools \
  --s3-bucket <CF_OUT_ArtifactBucketName> \
  --s3-prefix ledgerflow-sam-manual
```

The adapter stages only the handler source needed by the selected stack, then
executes `sam build` and `sam deploy`. `deploy-all.sh` invokes the same adapter
for every stack; only Stacks 02, 03, 05, and 07 need the build phase.

### Step 3 — Deploy storage

```bash
python3 cloudformation/sam_deploy.py \
  --environment dev \
  --region "$AWS_REGION" \
  --context-file cloudformation/build/deployment-context-dev.json \
  --stack 01-storage \
  --s3-bucket <CF_OUT_ArtifactBucketName> \
  --s3-prefix ledgerflow-sam-manual
```

Confirm that all table names and bucket names are added to the deployment
context.

### Step 4 — Deploy and ingest S3 Vectors

Deploy Stack 02 with SAM, then run the ingestion job:

```bash
python3 cloudformation/sam_deploy.py \
  --environment dev \
  --region "$AWS_REGION" \
  --context-file cloudformation/build/deployment-context-dev.json \
  --stack 02-s3-vectors \
  --s3-bucket <CF_OUT_ArtifactBucketName> \
  --s3-prefix ledgerflow-sam-manual
python3 cloudformation/ingest.py \
  --context-file cloudformation/build/deployment-context-dev.json \
  --region "$AWS_REGION" \
  --vector-dimension "${VECTOR_DIMENSION:-1024}" \
  --upload
```

Do not report hybrid retrieval as ready until the S3 Vectors text-search fields
have reached `ACTIVE` status and the smoke queries return expected evidence.

The expected deterministic seed counts after ingestion are:

```text
documents:         21
customers:        213
incidents:        113
invoices:         231
application_logs: 5150
deployments:       43
```

### Step 5 — Deploy tools

Deploy Stack 03 with SAM. Invoke each tool directly with an authorized test request and
confirm:

- tenant filtering works;
- read-only tools return structured results;
- tool output contains source IDs;
- a different tenant’s evidence is excluded;
- prompt-injection text is treated as untrusted content.

### Step 6 — Deploy AgentCore

Deploy Stack 04 with the enforced JSON policy defaults. Invoke the Runtime with
a test session and confirm that tool calls, memory calls, and Runtime traces
appear.

Run the policy regression suite. If a controlled observation-only rollout is
required, select `LOG_ONLY`; restore the enforced defaults after expected allow
and deny decisions are verified:

```bash
python3 cloudformation/sam_deploy.py --environment dev --region "$AWS_REGION" \
  --context-file cloudformation/build/deployment-context-dev.json \
  --stack 04-agentcore \
  --s3-bucket <CF_OUT_ArtifactBucketName> \
  --s3-prefix ledgerflow-sam-manual \
  --set GatewayPolicyMode=ENFORCE \
  --set PolicyEnforcementMode=ACTIVE
```

### Step 7 — Deploy workflow and observability

Deploy Stack 05 and Stack 06 through `sam_deploy.py`. Start a one-incident
workflow and verify:

- checkpoints are written;
- the report is saved;
- the workflow can resume from a checkpoint;
- traces and metrics contain the workflow/session ID;
- failures generate useful stack and application events.

To run the deterministic recovery test, sign in to the deployed console as an
administrator and open the **Dev tools** tab. Enter a unique workflow ID such
as `recovery-demo-1`, choose the checkpoint at which the Runtime should fail
(for example **Read application logs**), set the expiry, and click **Arm
failure plan**. The console copies that ID to the admin-only workflow field on
the **Investigation** tab; submit the case with the same ID.

The Runtime fails once after durably writing the selected checkpoint. Step
Functions retries the same workflow, the Runtime hydrates the checkpoint,
skips the completed stages, and finishes the investigation. The Dev tools
history changes from **ACTIVE** to **CONSUMED**. Click **Disarm** to cancel an
armed plan before it is claimed. This control is intentionally available only
in `dev` and `test`, and does not require changing CloudFormation for each
exercise.

#### Recovery mechanics

The failure plan is a runtime control, not a workflow input or a CloudFormation
toggle. The admin action writes an `ACTIVE` record to the
`DevToolsControlsTable`, including the exact `workflow_id`, checkpoint, creator,
and expiry. The table's `WorkflowStepIndex` lets the Runtime find the plan
without scanning unrelated workflows. Plans are limited to one active plan for
the same workflow and checkpoint and expire automatically.

Each Runtime stage follows this order:

1. collect the stage's result, if it has one;
2. write the state and completed-step set to `WorkflowCheckpointsTable`;
3. look for an active plan matching the current workflow and checkpoint;
4. atomically mark the plan consumed and emit a `failure_injection` event; and
5. raise the controlled failure after the checkpoint is durable.

The selected checkpoint therefore determines the next retry boundary:

| Console selection | Durable checkpoint | Retry begins with |
| --- | --- | --- |
| Request accepted | `started` | planning |
| Find incident | `incident_lookup` | invoice lookup |
| Check invoices | `invoice_lookup` | application-log lookup |
| Read application logs | `log_lookup` | deployment lookup |
| Trace deployment | `deployment_lookup` | hybrid evidence search |
| Hybrid evidence search | `retrieval` | verified-memory lookup |
| Recall verified memory | `memory_lookup` | report synthesis |
| Synthesize report | `report_synthesis` | idempotent publication/completion |

On retry, the Runtime uses the same workflow ID from the Step Functions payload
and AgentCore session ID. It validates the checkpoint schema and tenant/customer
ownership, acquires the conditional workflow lease, restores the saved plan and
records, and emits a `resumed` event with the completed steps. Already completed
Gateway, evidence, memory, and synthesis work is not invoked again. The final
checkpoint is `COMPLETED`, the report is stored under the workflow ID, and the
control remains **CONSUMED** as an audit record.

The controlled failure releases the lease before raising so the retry can
proceed immediately. A real process crash follows the same checkpoint protocol
but leaves the lease to expire normally. If two workers race to recover, the
workflow lease allows only one to continue. If the Dev Tools table is missing or
temporarily unavailable, the Runtime fails closed for the optional test hook and
continues normal investigations without injected failure.

### Step 8 — Deploy the web console

Deploy Stack 07 with SAM. Provide `BootstrapAdminEmail` for the first
administrator invite. Open the `WebApiUrl` output, sign in through Cognito, and
verify that the console shows:

- the investigation form;
- live checkpoint and tool-call events;
- the operator-visible safe Reasoning inspector;
- admin-only raw LLM prompt previews;
- the admin-only Prompt library tab, which displays native Bedrock draft
  content, edits and saves drafts, creates new prompts, lists immutable
  versions, and activates a selected runtime version;
- the admin-only Dev tools tab, which arms and disarms one-shot checkpoint
  failures and shows the control history;
- the Execution log popover with per-event payload details;
- the visual workflow timeline with hover/focus result traces for returned
  incident, invoice, log, deployment, memory, and vector-evidence records; and
- the final report, evidence IDs, and typed Evidence reasoning claims.

## 10. Smoke tests

The baseline end-to-end smoke test is:

```text
Request:
Investigate why customer ACME-482 received duplicate invoices after deployment 3.8.2.
Do not perform financial modifications without approval.
```

Expected evidence includes:

```text
INC-1042
INV-8821
INV-8822
LOG-8821
DEP-382
RUNBOOK-014
```

Expected behavior:

- the agent finds the duplicate invoice pair;
- the retry used a different idempotency key;
- deployment 3.8.2 is correlated;
- the runbook supports the idempotency finding;
- the report distinguishes facts from hypotheses;
- remediation is recommended but not executed;
- no financial write tool is called;
- all findings contain evidence references.

Run at least one expanded scenario as well. For example:

```text
Request:
Investigate the duplicate invoice after the payment retry timeout.
Customer: ACME-487
Incident: INC-1050
Tenant: tenant_acme
```

The expanded case should return `evidence_schema_version` equal to
`ledgerflow.evidence.v2`, include typed `evidence_claims`, and select the
case's prior deployment (`DEP-ACME-487`) rather than unrelated deployments
from the same tenant and version.

## 11. Update procedure

For a normal application update:

1. run unit, retrieval, authorization, and report tests;
2. build a new Runtime image;
3. push the image by immutable digest;
4. run `deploy.py --plan`;
5. run `sam_deploy.py` for the affected stacks;
6. inspect SAM's CloudFormation change-set and progress output for IAM, data,
   and replacement changes;
7. run the smoke and evaluation suites in development;
8. promote the same template and image digest after review.

Because stack values are passed by deployment context, changing an output name
requires updating the downstream JSON files and deployment tests, but does not
create an invisible CloudFormation import dependency.

## 12. Failure and rollback behavior

### Failed stack deployment

The orchestrator stops at the failed stack, captures stack events, and retains
the previous successful context. Fix the template or configuration and rerun
from that stack.

### Failed later stack

Earlier independent stacks remain intact. Do not redeploy or replace them unless
their templates changed. Reconstruct the context with `describe-stacks` if the
local context file is missing.

### Runtime image failure

Redeploy Stack 04 with the previous digest. Keep the previous Runtime endpoint
version available until the new version passes the smoke test.

### S3 Vectors failure

Do not delete the vector bucket as part of an automatic rollback. Vector buckets
must be empty before deletion, and their data is valuable. Retain the bucket,
fix the index or ingestion configuration, and rerun the data validation job.

### Partial deployment recovery

Rerun `./deploy-all.sh --skip-ingest` after a partial deployment. Stable stack
names and the deployment context make the command resumable. For one stack,
invoke `sam_deploy.py` directly; it hydrates all prior stack outputs before
running SAM, so a missing context cache does not require CloudFormation imports
or manual copying of ARNs. Use `deploy.py --from` only for a dependency preview.

## 13. Destruction procedure

Destroy stacks in reverse order:

```text
07-web-app
06-observability
05-workflow
04-agentcore
03-tools
02-s3-vectors
01-storage
00-security
```

Before deletion:

- export reports and evaluation results;
- preserve the deployment context;
- delete or archive vector data explicitly;
- empty buckets only through an approved cleanup command;
- confirm that no workflow is still running;
- confirm that no Runtime session is active.

Stateful data resources should default to `Retain` in development and
production until the cleanup process is proven.

## 14. Deployment acceptance criteria

- [ ] Every stack has a matching YAML and JSON file.
- [ ] `cloudformation/validate.py` passes.
- [ ] `deploy.py --stack 00-security --plan` resolves all first-stack values.
- [ ] Later stacks resolve after their predecessor outputs are in context.
- [ ] No template contains `Fn::ImportValue`.
- [ ] No template contains `AWS::CloudFormation::Stack`.
- [ ] No stack requires Service Catalog.
- [ ] No cross-account role is required.
- [ ] Stack outputs are available as `CF_OUT_*` values.
- [ ] S3 Vectors index is `ENHANCED` and full-text fields are `ACTIVE`.
- [ ] Runtime image is referenced by immutable digest.
- [ ] If enabled, model synthesis returns valid report JSON and only citations from the evidence packet.
- [ ] Gateway policies pass in enforced `ENFORCE`/`ACTIVE` mode (or an explicitly documented observation-only rollout is active).
- [ ] ACME-482 baseline smoke test passes.
- [ ] At least one expanded synthetic case, such as ACME-487 / INC-1050, passes.
- [ ] ACME-496 / INC-1059 shows context pressure with selected, full,
      citation-only, and omitted evidence counts in the console.
- [ ] Reports contain `ledgerflow.evidence.v2` typed evidence claims.
- [ ] The deterministic seed contains 21 documents, 113 incidents, 213 customers,
      231 invoices, 5,150 logs, and 43 deployments.
- [ ] Crash recovery test passes.
- [ ] No financial write tool is deployed.
- [ ] CloudFormation change sets are used for production updates.
- [ ] Deployment context contains no secrets.

## 15. References

- [AWS sample deployment pattern](https://github.com/aws-samples/service-catalog-framework-with-cross-account-codepipeline)
- [CloudFormation Outputs](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/outputs-section-structure.html)
- [CloudFormation stack deployment](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-cli-package.html)
- [S3 Vectors CloudFormation resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_S3Vectors.html)
- [AgentCore CloudFormation resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_BedrockAgentCore.html)
- [S3 Vectors hybrid search](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-hybrid-search.html)
