#!/usr/bin/env bash

# Build the Runtime image, package the Lambda artifacts, seed the AWS data
# stores, and deploy every LedgerFlow CloudFormation stack in dependency order.
#
# The script intentionally uses the existing Python deployment orchestrator so
# stack outputs continue to flow through the checked-in deployment context.
# It is safe to rerun after a partial deployment: each stack has a stable name
# and deploy.py hydrates predecessor outputs from CloudFormation.

set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ENVIRONMENT_NAME="${ENVIRONMENT_NAME:-dev}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
VECTOR_DIMENSION="${VECTOR_DIMENSION:-1024}"
EMBEDDING_MODEL_ID="${EMBEDDING_MODEL_ID:-amazon.titan-embed-text-v2:0}"
IMAGE_PLATFORM="${IMAGE_PLATFORM:-linux/arm64}"
RELEASE_ID="${RELEASE_ID:-$(date -u +%Y%m%d%H%M%S)}"
RUNTIME_IMAGE_TAG="${RUNTIME_IMAGE_TAG:-${ENVIRONMENT_NAME}-${RELEASE_ID}}"
WEB_ARTIFACT_KEY="${WEB_ARTIFACT_KEY:-lambda/web_app-${RELEASE_ID}.zip}"
CONTEXT_FILE="${CONTEXT_FILE:-$ROOT/cloudformation/build/deployment-context-${ENVIRONMENT_NAME}.json}"
SKIP_TESTS="${SKIP_TESTS:-0}"
SKIP_VALIDATION="${SKIP_VALIDATION:-0}"
SKIP_INGEST="${SKIP_INGEST:-0}"

usage() {
  cat <<'USAGE'
Usage: ./deploy-all.sh [options]

Build and deploy the complete LedgerFlow AWS environment in dependency order.
The command creates and executes CloudFormation change sets; use the existing
cloudformation/deploy.py commands when a review-only change-set workflow is
required.

Options:
  --environment NAME       CloudFormation environment (default: dev)
  --region REGION          AWS Region (default: us-east-1)
  --vector-dimension N     Titan embedding dimension (default: 1024)
  --release-id ID          Release identifier used for image/artifact names
  --skip-tests             Skip the local pytest suite
  --skip-validation        Skip CloudFormation contract validation
  --skip-ingest            Do not reload DynamoDB, S3, or S3 Vectors fixtures
  -h, --help               Show this help

Useful environment overrides are MODEL_ID, ENABLE_MODEL_REASONING,
GATEWAY_POLICY_MODE, POLICY_ENFORCEMENT_MODE, BOOTSTRAP_ADMIN_EMAIL,
BOOTSTRAP_ADMIN_TENANT_ID, DEPLOY_PYTHON, and CONTEXT_FILE.
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '\n==> %s\n' "$*"
}

need_value() {
  (($# >= 2)) || die "missing value for $1"
}

while (($# > 0)); do
  case "$1" in
    --environment)
      need_value "$@"
      ENVIRONMENT_NAME="$2"
      shift 2
      ;;
    --region)
      need_value "$@"
      AWS_REGION="$2"
      shift 2
      ;;
    --vector-dimension)
      need_value "$@"
      VECTOR_DIMENSION="$2"
      shift 2
      ;;
    --release-id)
      need_value "$@"
      RELEASE_ID="$2"
      RUNTIME_IMAGE_TAG="${ENVIRONMENT_NAME}-${RELEASE_ID}"
      WEB_ARTIFACT_KEY="lambda/web_app-${RELEASE_ID}.zip"
      shift 2
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    --skip-validation)
      SKIP_VALIDATION=1
      shift
      ;;
    --skip-ingest)
      SKIP_INGEST=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1 (use --help for usage)"
      ;;
  esac
done

export ENVIRONMENT_NAME AWS_REGION AWS_DEFAULT_REGION
export VECTOR_DIMENSION EMBEDDING_MODEL_ID
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$AWS_REGION}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

require_command aws
require_command docker
require_command python3

docker buildx version >/dev/null 2>&1 || die "docker buildx is required"

DEPLOY_PYTHON="${DEPLOY_PYTHON:-python3}"
command -v "$DEPLOY_PYTHON" >/dev/null 2>&1 || die "DEPLOY_PYTHON is not on PATH: $DEPLOY_PYTHON"
"$DEPLOY_PYTHON" -c 'import boto3' >/dev/null 2>&1 || die "boto3 is required; install it locally with: python3 -m pip install --user boto3"

aws_cli() {
  aws --region "$AWS_REGION" "$@"
}

python_cli() {
  "$DEPLOY_PYTHON" "$@"
}

run_deploy() {
  python_cli "$ROOT/cloudformation/deploy.py" \
    --environment "$ENVIRONMENT_NAME" \
    --region "$AWS_REGION" \
    --context-file "$CONTEXT_FILE" \
    "$@"
}

stack_output() {
  local stack_id="$1"
  local output_key="$2"
  local value
  value="$(aws_cli cloudformation describe-stacks \
    --stack-name "ledgerflow-${ENVIRONMENT_NAME}-${stack_id}" \
    --query "Stacks[0].Outputs[?OutputKey==\`${output_key}\`].OutputValue" \
    --output text)"
  [[ -n "$value" && "$value" != "None" ]] || die "missing ${output_key} output from ${stack_id}"
  printf '%s\n' "$value"
}

if [[ "$SKIP_TESTS" != 1 ]]; then
  log "Running the local test suite"
  python3 -m pytest
fi

if [[ "$SKIP_VALIDATION" != 1 ]]; then
  log "Validating CloudFormation contracts"
  python_cli "$ROOT/cloudformation/validate.py"
fi

log "Checking AWS identity"
aws_cli sts get-caller-identity

log "Deploying Stack 00: security and bootstrap"
run_deploy --stack 00-security --execute

ARTIFACT_BUCKET="$(stack_output 00-security ArtifactBucketName)"
RUNTIME_REPOSITORY_URI="$(stack_output 00-security RuntimeRepositoryUri)"
RUNTIME_REPOSITORY_NAME="${RUNTIME_REPOSITORY_URI##*/}"

log "Packaging and uploading Lambda artifacts"
python_cli "$ROOT/cloudformation/package_artifacts.py" \
  --bucket "$ARTIFACT_BUCKET" \
  --region "$AWS_REGION" \
  --key lambda/tool_handler.zip \
  --output "$ROOT/cloudformation/build/tool_handler.zip" \
  --vendor-dependencies \
  --upload

WEB_PACKAGE="$ROOT/cloudformation/build/web_app-${RELEASE_ID}.zip"
log "Packaging and uploading the web console"
python_cli "$ROOT/cloudformation/package_web.py" --output "$WEB_PACKAGE"
aws_cli s3 cp "$WEB_PACKAGE" "s3://${ARTIFACT_BUCKET}/${WEB_ARTIFACT_KEY}"

log "Building and pushing the Runtime image"
aws_cli ecr get-login-password | docker login --username AWS --password-stdin "$RUNTIME_REPOSITORY_URI"
docker buildx build \
  --platform "$IMAGE_PLATFORM" \
  --provenance=false \
  --tag "${RUNTIME_REPOSITORY_URI}:${RUNTIME_IMAGE_TAG}" \
  --push \
  "$ROOT"

RUNTIME_DIGEST="$(aws_cli ecr describe-images \
  --repository-name "$RUNTIME_REPOSITORY_NAME" \
  --image-ids "imageTag=${RUNTIME_IMAGE_TAG}" \
  --query 'imageDetails[0].imageDigest' \
  --output text | tr -d '\r')"
[[ "$RUNTIME_DIGEST" == sha256:* ]] || die "could not resolve the pushed image digest (got: $RUNTIME_DIGEST)"
RUNTIME_IMAGE_URI="${RUNTIME_REPOSITORY_URI}@${RUNTIME_DIGEST}"
printf 'Runtime image: %s\n' "$RUNTIME_IMAGE_URI"

log "Deploying Stack 01: operational storage"
run_deploy --stack 01-storage --execute

log "Deploying Stack 02: S3 Vectors"
run_deploy --stack 02-s3-vectors --execute

if [[ "$SKIP_INGEST" != 1 ]]; then
  log "Loading deterministic fixtures into DynamoDB, S3, and S3 Vectors"
  python_cli "$ROOT/cloudformation/ingest.py" \
    --context-file "$CONTEXT_FILE" \
    --region "$AWS_REGION" \
    --vector-dimension "$VECTOR_DIMENSION" \
    --embedding-model-id "$EMBEDDING_MODEL_ID" \
    --upload
else
  log "Skipping fixture ingestion"
fi

log "Deploying Stack 03: read-only tools"
run_deploy --stack 03-tools --execute

log "Deploying Stack 04: AgentCore Runtime, Gateway, Memory, and policy"
run_deploy --stack 04-agentcore --execute \
  --set "RUNTIME_IMAGE_URI=${RUNTIME_IMAGE_URI}"

log "Deploying Stack 05: workflow"
run_deploy --stack 05-workflow --execute

log "Deploying Stack 06: observability"
run_deploy --stack 06-observability --execute

WEB_OVERRIDES=(--set "WEB_ARTIFACT_KEY=${WEB_ARTIFACT_KEY}")
if [[ -n "${BOOTSTRAP_ADMIN_EMAIL:-}" ]]; then
  WEB_OVERRIDES+=(--set "BOOTSTRAP_ADMIN_EMAIL=${BOOTSTRAP_ADMIN_EMAIL}")
fi
if [[ -n "${BOOTSTRAP_ADMIN_TENANT_ID:-}" ]]; then
  WEB_OVERRIDES+=(--set "BOOTSTRAP_ADMIN_TENANT_ID=${BOOTSTRAP_ADMIN_TENANT_ID}")
fi

log "Deploying Stack 07: Cognito-protected web console"
run_deploy --stack 07-web-app --execute "${WEB_OVERRIDES[@]}"

WEB_API_URL="$(stack_output 07-web-app WebApiUrl)"
log "Deployment complete"
printf 'Environment: %s\n' "$ENVIRONMENT_NAME"
printf 'Region: %s\n' "$AWS_REGION"
printf 'Web console: %s\n' "$WEB_API_URL"
printf 'Deployment context: %s\n' "$CONTEXT_FILE"
