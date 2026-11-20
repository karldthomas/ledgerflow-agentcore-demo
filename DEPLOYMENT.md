# LedgerFlow Deployment Guide

This is the operator runbook for deploying, verifying, updating, recovering,
and removing LedgerFlow. The [tutorial](TUTORIAL.md) explains the system
architecture. The [deployment manifest](cloudformation/deployment.yaml),
[orchestrator](cloudformation/orchestrate.py), and CloudFormation templates are
the source of truth for stack order and configuration.

## Prerequisites

- Python 3.12+, `boto3`, AWS CLI, AWS SAM CLI, and Docker with Buildx.
- AWS credentials for the target account and permission to deploy the stacks.
  The deployment scripts use the standard AWS credential chain; they do not
  select a profile themselves.
- Bedrock model access and AgentCore/S3 Vectors availability in the selected
  Region.
- An administrator email for the first console deployment, if an initial
  Cognito admin invite is needed.

The development environment uses `us-east-1` by default. Confirm the account
and Region before deploying:

```bash
aws sts get-caller-identity
aws configure get region
```

## Stack order

The manifest defines eight independent stacks. Stack outputs flow to later
stacks through the deployment context; there are no nested stacks or
CloudFormation exports/imports.

| Stack | Purpose |
|---|---|
| `00-security` | KMS, artifact bucket, ECR repository, and deployment roles |
| `01-storage` | Operational data tables and raw/report buckets |
| `02-s3-vectors` | Evidence vector bucket, index, and index configuration |
| `03-tools` | Read-only Gateway tool Lambdas |
| `04-agentcore` | Runtime, Gateway, Policy, Memory, and prompts |
| `05-workflow` | Step Functions investigation workflow and post-run audit |
| `06-observability` | Logs, metrics, dashboard, and alarm |
| `07-web-app` | Cognito-protected console and API |

See [`cloudformation/deployment.yaml`](cloudformation/deployment.yaml) for the
current dependencies and lifecycle steps, and `cloudformation/stacks/` for
each stack's parameters and resources.

## Deploy the development environment

Install the Python deployment dependency if needed:

```bash
python3 -m pip install --user boto3
```

Set `AWS_PROFILE` or other standard AWS credential variables in your shell if
needed. Then deploy:

```bash
BOOTSTRAP_ADMIN_EMAIL=admin@example.com \
  ./deploy-all.sh --environment dev --region us-east-1
```

`BOOTSTRAP_ADMIN_EMAIL` is optional after the initial Cognito administrator
has been created. The script runs tests and deployment-contract validation,
checks AWS identity, builds and pushes the Runtime image by digest, ingests the
synthetic data, and deploys all eight stacks through SAM. It prints the console
URL when the web stack completes.

The script supports `--skip-ingest` when fixtures are already loaded, and
`--skip-tests` / `--skip-validation` for controlled follow-up deployments. A
normal release should keep the checks enabled. See `./deploy-all.sh --help`
for release ID, image, and other options.

## Preview and validate

Validate the repository's stack contracts without deploying:

```bash
python3 cloudformation/validate.py
```

Resolve and review the deployment plan:

```bash
python3 cloudformation/deploy.py \
  --environment dev \
  --region us-east-1 \
  --plan
```

The plan command does not deploy stacks. The validator checks matching
YAML/JSON files, parameter and output dependencies, and forbidden stack
coupling. The manifest and JSON configuration files define the parameter
resolution details; they are not repeated here.

## Deploy or update one stack

Use the full orchestrator for normal releases. For a controlled one-stack
update, use the SAM adapter after its predecessor stacks exist:

```bash
python3 cloudformation/sam_deploy.py \
  --environment dev \
  --region us-east-1 \
  --stack 07-web-app \
  --s3-bucket <artifact-bucket-name> \
  --s3-prefix manual-update
```

Get the artifact bucket name from the `00-security` stack output. The adapter
hydrates prior stack outputs from CloudFormation, builds Lambda-bearing stacks,
and submits the stack through SAM's change-set flow. Use `--set KEY=VALUE` for
an explicit parameter override. For the supported options, run:

```bash
python3 cloudformation/sam_deploy.py --help
```

## Verify the deployment

Confirm the web stack is complete and retrieve its URL:

```bash
aws cloudformation describe-stacks \
  --stack-name ledgerflow-dev-07-web-app \
  --query 'Stacks[0].[StackStatus,Outputs[?OutputKey==`WebApiUrl`].OutputValue]' \
  --output table \
  --region us-east-1
```

Sign in through Cognito and run the baseline investigation:

```text
Customer: ACME-482
Incident: INC-1042
Tenant: tenant_acme
Request: Investigate why ACME-482 received duplicate invoices after deployment 3.8.2.
```

Confirm the report cites the incident, both invoices, the retry log, deployment,
and runbook; labels typed claims; and does not invoke a financial-write tool.
Also run `ACME-487` / `INC-1050` as an expanded case. For context compaction,
run `ACME-496` / `INC-1059` and inspect the Compact context stage in the
console. The current development seed contains 21 documents, 113 incidents,
213 customers, 231 invoices, 5,150 application logs, and 43 deployments.

## Recovery and rollback

If a stack fails, SAM reports the CloudFormation failure and that stack rolls
back according to CloudFormation behavior. Earlier independent stacks remain
in place. Fix the cause, then rerun `./deploy-all.sh`; stable stack names and
CloudFormation outputs let it resume. Use `--skip-ingest` if fixture ingestion
already completed successfully.

If the local deployment context is missing, the single-stack adapter can
rebuild prior stack outputs from CloudFormation. For a one-stack retry, use
`sam_deploy.py --stack <stack-id>` with the required SAM artifact bucket.

For a failed Runtime image release, redeploy Stack 04 with the last known-good
`RuntimeImageUri` digest:

```bash
python3 cloudformation/sam_deploy.py \
  --environment dev \
  --region us-east-1 \
  --stack 04-agentcore \
  --set RuntimeImageUri=<previous-digest-qualified-image-uri>
```

For S3 Vectors failures, preserve the vector bucket and its data while
investigating. Do not treat deleting the stack as a data cleanup procedure.

To exercise checkpoint recovery, use the admin-only **Dev tools** tab in a
`dev` or `test` environment: arm a one-shot failure for a workflow ID and
checkpoint, then submit an investigation with that same ID. The retry should
resume from the durable checkpoint. The [tutorial's recovery section](TUTORIAL.md#7-hydrate-state-after-interruption)
explains the workflow behavior.

## Remove an environment

There is no automatic teardown command. Before removing stacks, export reports
and any evaluation data, stop active workflows, and decide what to do with
retained data. Delete stacks in reverse dependency order:

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

Many storage, vector, and artifact resources have `DeletionPolicy: Retain`.
Deleting their stacks may leave billable resources behind. Inventory and
explicitly clean up retained resources only after confirming the data is no
longer needed. Keep the deployment context and record the resources removed.

## Source files

- [`cloudformation/deployment.yaml`](cloudformation/deployment.yaml): stack order and lifecycle steps.
- [`cloudformation/orchestrate.py`](cloudformation/orchestrate.py): full deployment workflow.
- [`cloudformation/deploy.py`](cloudformation/deploy.py): plan resolution and output context.
- [`cloudformation/sam_deploy.py`](cloudformation/sam_deploy.py): one-stack SAM deployment.
- [`cloudformation/validate.py`](cloudformation/validate.py): repository-only deployment validation.
- [`cloudformation/stacks/`](cloudformation/stacks/): CloudFormation resources and parameters.
