#!/usr/bin/env bash
set -euo pipefail

# Create/update IAM policy and role for extension Handlers Lambda (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT.

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SERVICE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/installer/service"
POLICY_FILE="$SERVICE_DIR/${EXTENSION_NAME}-handlers-iam-policy.json"
POLICY_NAME="$(echo "${EXTENSION_NAME:0:1}" | tr '[:lower:]' '[:upper:]')${EXTENSION_NAME:1}HandlersPolicy"
ROLE_NAME="${EXTENSION_NAME}-handlers-role"
AWS_REGION="${AWS_REGION:-us-east-1}"

if [[ ! -f "$POLICY_FILE" ]]; then
  echo "ERROR: Policy file not found: $POLICY_FILE" >&2
  exit 1
fi

echo "=========================================="
echo "IAM Role Setup: $EXTENSION_NAME"
echo "=========================================="
echo "Policy: $POLICY_NAME"
echo "Role: $ROLE_NAME"
echo ""

command -v aws >/dev/null 2>&1 || { echo "ERROR: AWS CLI not found" >&2; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "ERROR: AWS credentials not configured" >&2; exit 1; }

AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT}:policy/${POLICY_NAME}"

if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  echo "Updating policy..."
  aws iam create-policy-version --policy-arn "$POLICY_ARN" --policy-document "file://${POLICY_FILE}" --set-as-default
else
  echo "Creating policy..."
  aws iam create-policy --policy-name "$POLICY_NAME" --policy-document "file://${POLICY_FILE}" \
    --description "IAM policy for ${EXTENSION_NAME} Handlers Lambda"
fi

TRUST_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "Updating role trust policy..."
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST_POLICY"
else
  echo "Creating role..."
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST_POLICY" \
    --description "IAM role for ${EXTENSION_NAME} Handlers Lambda"
fi

aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

echo ""
echo "Done. Role ARN: arn:aws:iam::${AWS_ACCOUNT}:role/${ROLE_NAME}"
echo "Deploy: python3 dev/extension-service/run.py $EXTENSION_NAME deploy"
echo ""
