# LedgerFlow Incident Response Agent

## Business Context

LedgerFlow is a fictional SaaS company that processes subscription payments for approximately 2,000 business customers. Its support and engineering teams receive hundreds of payment-related incidents each month.

Investigations currently require people to correlate support tickets, invoices, payment events, application logs, deployment history, and operational guidance. The business needs a consistent way to investigate these incidents, explain the findings, and document the outcome without making an unsafe financial change.

## Business Problem

LedgerFlow needs an incident-response assistant that can help an authorized operator:

- investigate a reported payment incident;
- determine which customers, invoices, and services are affected;
- identify a likely root cause from the available operational evidence;
- distinguish confirmed facts from hypotheses and open questions;
- recommend corrective actions;
- preserve an auditable explanation of how the conclusion was reached; and
- continue an interrupted investigation without repeating a consequential action.

The assistant is an investigation aid. It is not authorized to change invoices, issue refunds, cancel payments, charge customers, or modify financial records.

## Users and Stakeholders

### Support and operations users

Need a fast, understandable answer to a customer or internal incident. They need the timeline, affected scope, evidence, root cause, recommendations, and remaining risks in one report.

### Engineering and incident-response users

Need to correlate payment behavior with deployments, logs, configuration, and runbooks. They need enough detail to reproduce the reasoning and plan a fix.

### Administrators

Need to manage access, review investigation history, and test recovery behavior without exposing customer data across tenants.

### Customers and finance stakeholders

Need confidence that reported findings are traceable, that uncertainty is clearly stated, and that no financial action occurs without approval.

## Required Use Cases

The assistant must support investigations for:

1. Duplicate invoices after a retry or timeout.
2. Payment failures returned by a payment provider.
3. Payment timeouts whose final provider outcome is unknown or later reconciled.
4. Merchant or payment configuration errors.
5. Follow-up verification of an existing incident or explanation of an operational rule.

For every use case, the assistant must return a report tied to the requested customer and incident. It must use only evidence the operator is authorized to see.

## Canonical Incident: ACME-482

The primary business scenario is:

> Investigate why customer ACME-482 received duplicate invoices following the November deployment. Identify the root cause, provide supporting evidence, and recommend corrective actions. Do not perform financial modifications without approval.

The investigation is expected to connect these business facts:

1. A payment renewal experienced a timeout.
2. The retry created a second payment operation instead of safely reusing the original operation identity.
3. Two invoices were issued for one renewal.
4. Deployment `3.8.2` introduced the changed retry behavior.
5. The retry runbook required the original operation identity to be reused and the provider outcome to be reconciled before retrying.

The expected conclusion is that the retry-key regression caused a duplicate financial operation after a timeout. The report should recommend restoring safe retry behavior, reconciling affected records, and adding regression coverage. It must not execute those actions.

## Required Investigation Report

Each completed investigation must include:

- an executive summary;
- the affected customers, invoices, subscriptions, and services;
- an incident timeline;
- the suspected root cause and its confidence;
- supporting evidence and where each piece came from;
- verified findings separated from hypotheses;
- recommended corrective actions;
- recovery history if the investigation was interrupted;
- unresolved questions and remaining risks; and
- a clear statement of whether any financial action was taken.

The report must be understandable to an operations user and detailed enough for an engineer to validate.

## Business Rules and Guardrails

The assistant must:

- keep each investigation within the operator's tenant and customer scope;
- never treat an unrelated customer's record as supporting evidence;
- cite the records that support each material finding;
- preserve the distinction between fact, relationship, hypothesis, and recommendation;
- state when evidence is missing, conflicting, stale, or inconclusive;
- require human approval before any financial remediation;
- remain read-only for the proof of concept;
- maintain an investigation history that an operator can review; and
- avoid duplicating a consequential action if an investigation is resumed.

A prompt, report, or recommendation must not override these rules.

## Scenario Coverage

The synthetic business scenarios must include:

| Scenario | Required business behavior |
| --- | --- |
| `ACME-482` | Explain the canonical duplicate-invoice incident and identify the retry-key regression |
| `ACME-487` | Identify a duplicate invoice caused by a changed retry key |
| `ACME-496` | Produce a complete report from a high-volume incident history without losing required findings |
| Payment failure cases | Distinguish provider rejection from timeout or configuration problems |
| Timeout cases | Separate unresolved outcomes from safely reconciled outcomes |
| Configuration cases | Identify the configuration issue and recommend an approved correction path |

The scenarios are synthetic and exist to make the requirements repeatable. They do not represent real customer or financial data.

## Acceptance Criteria

The proof of concept meets the business requirements when:

1. An authorized operator can submit an investigation for each supported incident type.
2. The ACME-482 report identifies the duplicate renewal, timeout, changed retry key, and deployment-related cause.
3. Every material conclusion is supported by traceable evidence.
4. Facts, hypotheses, recommendations, and unresolved questions are clearly separated.
5. A user cannot receive another tenant's evidence through an investigation.
6. The assistant does not perform financial writes.
7. An interrupted investigation can continue with its prior progress intact.
8. Resuming an investigation does not duplicate a consequential operation or report.
9. An operator can review the investigation timeline and final report.
10. The same synthetic scenario produces a repeatable, auditable outcome.

## Scope Boundaries

This case study covers incident investigation and reporting. It does not require:

- refunds, credits, cancellations, charges, invoice edits, or other financial writes;
- autonomous approval of remediation;
- a general-purpose support chatbot;
- real customer data;
- a production availability or multi-region guarantee; or
- claims about cost, latency, or accuracy beyond the acceptance criteria above.

The business outcome is a trustworthy investigation record that helps people make the next decision safely.
