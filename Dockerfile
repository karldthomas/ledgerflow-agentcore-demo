FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LEDGERFLOW_BACKEND=aws \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ledgerflow ./ledgerflow
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir boto3 .

EXPOSE 8080
CMD ["python", "-m", "ledgerflow.runtime_server"]
