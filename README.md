# LedgerFlow Incident Response Agent

This case study and its implementation were created with substantial assistance from AI. AI generated much of the code and helped develop the accompanying materials, while I directed the architecture, made the design decisions, and reviewed the results. This project is intended as a tutorial.

An AWS-deployed AI agent for investigating financial incidents using tenant-scoped evidence, durable workflows, and auditable reports.

Built as a deployable proof of concept for [`CASE_STUDY.md`](CASE_STUDY.md), LedgerFlow investigates duplicate invoices, payment failures, timeouts, and configuration errors. The canonical walkthrough uses the synthetic `ACME-482` incident.

## Features

- AgentCore Runtime, Gateway, and Memory for agent execution, authorized read-only tools, and tenant-isolated context.
- Hybrid retrieval using S3 Vectors, Titan embeddings, BM25, and reciprocal-rank fusion.
- Evidence-grounded reasoning with typed claims, source citations, and deterministic validation.
- Durable execution using Step Functions, DynamoDB checkpoints, workflow leases, and idempotent report publication.
- Versioned prompts managed through Amazon Bedrock Prompt Management.
- Operator console with investigation timelines, reasoning summaries, audit results, and reports.
- Recovery testing through admin-only failure injection and checkpoint-based retries.

The model handles investigation planning and report synthesis. Authorization, retrieval boundaries, evidence validation, and workflow state remain deterministic. No financial-write tools are exposed.

## Architecture

```mermaid
flowchart TD
    UI["Web Console / API"] --> SFN["Step Functions"]
    SFN --> RT["AgentCore Runtime"]
    RT <--> MEM["AgentCore Memory"]
    RT --> GW["AgentCore Gateway"]
    GW --> TOOLS["Tenant-Scoped Lambda Tools"]
    TOOLS --> DB["DynamoDB"]
    TOOLS --> VEC["S3 Vectors"]
    RT --> STATE["DynamoDB Checkpoints"]
    RT --> REPORT["S3 Reports"]
    RT --> PM["Bedrock Prompt Management"]
    RT --> CW["CloudWatch"]
```

Infrastructure is provisioned through eight independent CloudFormation stacks using AWS SAM.

## Deployment

Prerequisites: Python 3.12+, AWS CLI, AWS SAM CLI, Docker with Linux/arm64 Buildx support, and boto3.

Configure AWS credentials and Region using standard environment variables.

Validate and preview the deployment without creating resources:

```bash
python3 cloudformation/validate.py
python3 cloudformation/deploy.py --environment dev --plan
```

Deploy the complete AWS environment:

```bash
python3 -m pip install --user boto3
BOOTSTRAP_ADMIN_EMAIL=admin@example.com ./deploy-all.sh --environment dev
```

The script builds the Runtime container, deploys all eight stacks, loads synthetic investigation data, and prints the Cognito-protected console URL.

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for deployment configuration, stack dependencies, and manual procedures.

## Demo and Recovery Testing

The deployment includes synthetic tenant-scoped incidents covering duplicate invoices, payment failures, timeouts, and configuration errors.

- `ACME-482`: Canonical duplicate-invoice investigation.
- `ACME-487`: Duplicate invoice caused by a changed retry key.
- `ACME-496`: Large evidence set for context-compaction testing.

In dev and test, administrators can use the console’s Dev tools tab to inject a one-shot failure after a checkpoint. Step Functions retries the workflow, completed stages are skipped, and report publication remains idempotent.

## Using the Web Console

### Sign in

Open the deployed console URL and sign in with an administrator-provisioned account. The console uses Cognito for access control; self-service account creation is disabled.

![LedgerFlow console sign-in](images/control-room-sign-in.png)

### Start an investigation

In the **Investigation** tab, enter the customer ID and tenant, add an incident ID if available, choose the task type, and describe the investigation request. The workflow ID is optional and is mainly used for administrator recovery testing. Select **Run investigation** to start the read-only workflow.

![Investigation workspace](images/investigation-workspace.png)

### Follow the evidence

The active run timeline shows each investigation stage and its status. Select a stage to inspect the tool calls and returned data. The invoice example shows the `get_customer_invoices` operation and a preview of its results.

![Invoice tool evidence trace](images/invoice-tool-evidence-trace.png)

After completion, the evidence-grounded report includes an executive summary, root cause, timeline, recommended actions, unresolved questions, and linked evidence. The pre-publication validation panel reports whether deterministic checks passed.

![Evidence-grounded report](images/evidence-grounded-report.png)

### Manage runtime prompts

Administrators can open **Prompt library** to review prompts used by investigation stages, including planning, report synthesis, and context compaction. Each stage shows its Bedrock draft and the immutable version selected for runtime use.

![Planning prompt](images/prompt-library-planning-stage.png)

![Report synthesis prompt](images/prompt-library-report-synthesis-stage.png)

![Context compaction prompt](images/prompt-library-context-compaction-stage.png)

Saving a draft changes the Bedrock draft. To use that change in investigations, create an immutable version and select it for the runtime stage. A standalone prompt draft is unassigned and has no effect on investigations until a version is assigned to a runtime stage. Keep `{{input}}` in a runtime prompt's user template so investigation data reaches the model.

### Test checkpoint recovery

In development and test environments, administrators can use **Dev tools** to arm a one-shot failure after a selected checkpoint. Enter a workflow ID, choose the checkpoint and expiry, then arm the plan. Run an investigation with that same workflow ID; the first attempt fails after persisting the checkpoint, and a retry resumes from it.

![Developer failure injection controls](images/dev-tools-failure-injection.png)

## Verification

```bash
python3 -m pytest
python3 cloudformation/validate.py
bash -n deploy-all.sh
node --check frontend/app.js
```

## Repository Structure

| Path | Purpose |
| --- | --- |
| `ledgerflow/` | Agent Runtime, planning, retrieval, evidence reasoning, and report synthesis |
| `cloudformation/` | Infrastructure stacks, deployment orchestration, and fixture ingestion |
| `lambda/` | Gateway tools and web application API |
| `frontend/` | Investigation and administration console |
| `tests/` | Runtime, retrieval, recovery, and infrastructure contract tests |
| `deploy-all.sh` | End-to-end AWS deployment |

## Documentation

- [`CASE_STUDY.md`](CASE_STUDY.md): Business context, requirements, scenarios, and acceptance criteria.
- [`TUTORIAL.md`](TUTORIAL.md): Technology choices, implementation details, and verification guide.
- [`DEPLOYMENT.md`](DEPLOYMENT.md): AWS deployment and operational procedures.

## License

MIT
