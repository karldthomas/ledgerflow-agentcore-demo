# LedgerFlow Agent Notes

## Repository purpose

LedgerFlow is a proof-of-concept incident-response agent. It investigates the
synthetic `ACME-482` duplicate-invoice incident and a larger set of tenant-scoped
timeout, payment-failure, configuration-error, and duplicate-invoice scenarios.
The local implementation is deterministic so retrieval, authorization, memory,
context management, schema-driven evidence reasoning, and workflow recovery can
be tested without AWS credentials.

The AWS deployment adapts the same orchestration boundary to Amazon Bedrock
AgentCore and related managed services.

## Architecture

```mermaid
flowchart TB
    User["Support engineer / API caller"]

    subgraph Local["Local deterministic POC"]
        CLI["ledgerflow CLI"]
        Agent["InvestigationAgent"]
        Router["Task-aware retrieval router"]
        Retrieval["Hybrid retrieval<br/>keyword + vector + RRF + reranking"]
        Context["Context builder<br/>compaction + token budget + cache"]
        Memory["Memory service"]
        Tools["Read-only tools<br/>tenant authorization"]
        Checkpoints["Checkpoint store<br/>recovery + idempotency"]
        SQLite[("SQLite<br/>synthetic incidents, invoices,<br/>logs, deployments, documents")]
        LocalReport["Evidence-grounded incident report"]

        CLI --> Agent
        Agent --> Router
        Router --> Retrieval
        Agent --> Context
        Agent --> Memory
        Agent --> Tools
        Agent --> Checkpoints
        Retrieval --> SQLite
        Tools --> SQLite
        Memory --> SQLite
        Checkpoints --> SQLite
        Context --> LocalReport
        Agent --> LocalReport
    end

    subgraph AWS["AWS / AgentCore deployment"]
        Console["Cognito-protected web console"]
        Workflow["Step Functions<br/>durable workflow"]
        Runtime["AgentCore Runtime<br/>LedgerFlow cloud agent"]
        Model["Optional Bedrock Converse<br/>planner + report synthesizer"]
        ACMemory["AgentCore Memory"]
        Gateway["AgentCore Gateway<br/>MCP + IAM/Cedar policy"]
        LambdaTools["Read-only Lambda tools"]
        OperationalDB[("DynamoDB<br/>customers, incidents, invoices,<br/>logs, deployments")]
        Embeddings["Titan text embeddings"]
        Vectors[("S3 Vectors<br/>BM25 + vector search + RRF")]
        State[("DynamoDB<br/>checkpoints + operation ledger")]
        Reports[("S3<br/>investigation reports")]
        Observability["CloudWatch / AgentCore<br/>logs, metrics, traces"]

        Console --> Workflow
        Console --> Runtime
        Workflow --> Runtime
        Runtime <--> Model
        Runtime <--> ACMemory
        Runtime --> Gateway
        Gateway --> LambdaTools
        LambdaTools --> OperationalDB
        LambdaTools --> Embeddings
        Embeddings --> Vectors
        LambdaTools --> Vectors
        Runtime --> State
        Runtime --> Reports
        Runtime -.-> Observability
        Workflow -.-> Observability
        Gateway -.-> Observability
        LambdaTools -.-> Observability
    end

    User --> CLI
    User --> Console

    Safety["Safety boundary:<br/>the model may plan and summarize;<br/>authorization, evidence validation,<br/>workflow state, and financial-write denial<br/>remain application-owned."]
    Runtime -.-> Safety
    Agent -.-> Safety
```

## Non-negotiable boundaries

- Keep investigation tools read-only. The POC must reject financial mutations.
- Enforce tenant authorization in application/tool code; do not rely on model
  instructions to select or protect tenants.
- Emit typed evidence claims under `ledgerflow.evidence.v2`; every finding and
  report citation must reference an ID from the authorized evidence packet.
- Preserve invariants during context compaction, especially no financial
  writes, evidence requirements, and approval requirements for remediation.
- Persist workflow checkpoints and operation IDs so interrupted investigations
  can resume without duplicating report publication.
- Treat memory as supporting context. Transient memory failures must not hide
  primary operational evidence.

## Useful commands

```bash
python3 -m ledgerflow.cli seed --reset
python3 -m ledgerflow.cli investigate ACME-482
python3 -m ledgerflow.cli investigate ACME-487
python3 -m pytest
```

The deterministic seed currently contains 21 documents, 112 incidents, 212
customers, 215 invoices, 5,030 application logs, and 42 deployments. The
additional `tenant_acme` cases are defined in `ledgerflow/db.py`.

To exercise recovery:

```bash
python3 -m ledgerflow.cli investigate ACME-482 \
  --db /tmp/ledgerflow-recovery.db \
  --workflow-id demo-recovery \
  --reset \
  --fail-after-step log_lookup

python3 -m ledgerflow.cli investigate ACME-482 \
  --db /tmp/ledgerflow-recovery.db \
  --workflow-id demo-recovery
```

AWS infrastructure is validated and deployed through the scripts in
`cloudformation/`; changes should remain explicit and use the documented
CloudFormation stack order.
