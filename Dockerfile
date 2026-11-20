FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ledgerflow/__init__.py \
     ledgerflow/cloud_agent.py \
     ledgerflow/context_compaction.py \
     ledgerflow/evidence_reasoning.py \
     ledgerflow/gateway.py \
     ledgerflow/models.py \
     ledgerflow/planner.py \
     ledgerflow/prompting.py \
     ledgerflow/reasoning.py \
     ledgerflow/runtime_entry.py \
     ledgerflow/runtime_server.py \
     ./ledgerflow/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir boto3 .

EXPOSE 8080
CMD ["python", "-m", "ledgerflow.runtime_server"]
