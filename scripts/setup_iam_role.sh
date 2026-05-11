#!/usr/bin/env bash
set -euo pipefail

# Create/update IAM policy and role for extension Handlers Lambda.
#
# The policy grants the backend Lambda the minimum permissions to invoke ECS tasks
# for this extension: ecs:RunTask on the task definition, iam:PassRole on the ECS
# roles, and s3:PutObject/GetObject on the results bucket.
#
# If extensions/<ext>/installer/service/<ext>-handlers-iam-policy.json exists it is
# used as-is (custom overrides). Otherwise the policy is generated dynamically from
# the extension name and AWS account — no file needs to be committed.
#
# Requires: EXTENSION_NAME, WORKSPACE_ROOT
# Optional: AWS_REGION, AWS_PROFILE

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

command -v aws >/dev/null 2>&1 || { echo "ERROR: AWS CLI not found" >&2; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "ERROR: AWS credentials not configured" >&2; exit 1; }

AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT}:policy/${POLICY_NAME}"

echo "=========================================="
echo "IAM Role Setup: $EXTENSION_NAME"
echo "Region: $AWS_REGION   Account: $AWS_ACCOUNT"
echo "Policy: $POLICY_NAME"
echo "Role:   $ROLE_NAME"
echo "=========================================="
echo ""

# Resolve or generate the policy document
GENERATED_POLICY_FILE=""
if [[ -f "$POLICY_FILE" ]]; then
  echo "Using custom policy file: $POLICY_FILE"
  EFFECTIVE_POLICY_FILE="$POLICY_FILE"
else
  echo "Generating policy for $EXTENSION_NAME (no custom file at $POLICY_FILE)"
  GENERATED_POLICY_FILE=$(mktemp --suffix=.json)
  python3 - <<PY
import json

account = "$AWS_ACCOUNT"
region  = "$AWS_REGION"
ext     = "$EXTENSION_NAME"

policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ECSRunTask",
            "Effect": "Allow",
            "Action": ["ecs:RunTask"],
            "Resource": f"arn:aws:ecs:{region}:{account}:task-definition/{ext}-handlers-ecs:*",
        },
        {
            "Sid": "ECSPassRole",
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": [
                f"arn:aws:iam::{account}:role/{ext}-handlers-ecs-execution",
                f"arn:aws:iam::{account}:role/{ext}-handlers-ecs-task",
            ],
            "Condition": {
                "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
            },
        },
        {
            "Sid": "ECSHandshakeS3",
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject"],
            "Resource": f"arn:aws:s3:::{ext}-handlers-ecs-{account}/*",
        },
    ],
}
with open("$GENERATED_POLICY_FILE", "w") as f:
    json.dump(policy, f, indent=2)
print("Policy generated.")
PY
  EFFECTIVE_POLICY_FILE="$GENERATED_POLICY_FILE"
fi

# Create or update the managed policy
if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  echo "Updating policy $POLICY_NAME..."
  aws iam create-policy-version \
    --policy-arn "$POLICY_ARN" \
    --policy-document "file://${EFFECTIVE_POLICY_FILE}" \
    --set-as-default >/dev/null
else
  echo "Creating policy $POLICY_NAME..."
  aws iam create-policy \
    --policy-name "$POLICY_NAME" \
    --policy-document "file://${EFFECTIVE_POLICY_FILE}" \
    --description "IAM policy for ${EXTENSION_NAME} Handlers Lambda" >/dev/null
fi

[[ -n "$GENERATED_POLICY_FILE" ]] && rm -f "$GENERATED_POLICY_FILE"

# Create or update the Lambda execution role
TRUST_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "Updating role trust policy for $ROLE_NAME..."
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST_POLICY" >/dev/null
else
  echo "Creating role $ROLE_NAME..."
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --description "IAM role for ${EXTENSION_NAME} Handlers Lambda" >/dev/null
fi

aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN" 2>/dev/null || true
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

echo ""
echo "Done. Role ARN: arn:aws:iam::${AWS_ACCOUNT}:role/${ROLE_NAME}"
echo ""
