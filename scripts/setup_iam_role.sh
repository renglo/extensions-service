#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# Create/update IAM policy and role for extension Handlers Lambda.
#
# The policy grants the backend Lambda the minimum permissions to invoke ECS tasks
# for this extension: ecs:RunTask on the task definition, iam:PassRole on the ECS
# roles, and s3:PutObject/GetObject on the results bucket.
#
# Policy document is always generated from the extension name and AWS account
# (ECS invoke + S3 handshake). No per-extension IAM policy JSON file is used.
#
# Also attaches {ext}_tt_policy to the Lambda handlers role so the handler code
# can access DynamoDB, S3, Cognito, SES, EventBridge, WebSocket, etc.
#
# Requires: EXTENSION_NAME, WORKSPACE_ROOT
# Optional: AWS_REGION, AWS_PROFILE

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
POLICY_NAME="$(echo "${EXTENSION_NAME:0:1}" | tr '[:lower:]' '[:upper:]')${EXTENSION_NAME:1}HandlersPolicy"
ROLE_NAME="${EXTENSION_NAME}-handlers-role"
TT_POLICY_NAME="${EXTENSION_NAME}_tt_policy"
AWS_REGION="${AWS_REGION:-us-east-1}"

command -v aws >/dev/null 2>&1 || { echo "ERROR: AWS CLI not found" >&2; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "ERROR: AWS credentials not configured" >&2; exit 1; }

AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT}:policy/${POLICY_NAME}"
TT_POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT}:policy/${TT_POLICY_NAME}"

echo "=========================================="
echo "IAM Role Setup: $EXTENSION_NAME"
echo "Region: $AWS_REGION   Account: $AWS_ACCOUNT"
echo "Policy: $POLICY_NAME"
echo "Role:   $ROLE_NAME"
echo "=========================================="
echo ""

# Generate the policy document
GENERATED_POLICY_FILE=$(mktemp --suffix=.json)
echo "Generating policy for $EXTENSION_NAME"
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

# Create or update the managed policy
if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  echo "Updating policy $POLICY_NAME..."
  aws iam create-policy-version \
    --policy-arn "$POLICY_ARN" \
    --policy-document "file://${EFFECTIVE_POLICY_FILE}" \
    --set-as-default >/dev/null
else
  echo "Creating policy $POLICY_NAME..."
  reglo_create_iam_policy "$POLICY_NAME" "$EFFECTIVE_POLICY_FILE" >/dev/null
fi

[[ -n "$GENERATED_POLICY_FILE" ]] && rm -f "$GENERATED_POLICY_FILE"

# Create or update the Lambda execution role
TRUST_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "Updating role trust policy for $ROLE_NAME..."
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST_POLICY" >/dev/null
else
  echo "Creating role $ROLE_NAME..."
  reglo_create_iam_role "$ROLE_NAME" "$TRUST_POLICY" >/dev/null
fi
reglo_ensure_iam_role_description "$ROLE_NAME"

aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN" 2>/dev/null || true
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

# Attach the platform runtime policy so handler code can access DynamoDB, S3, Cognito, SES, etc.
if aws iam get-policy --policy-arn "$TT_POLICY_ARN" >/dev/null 2>&1; then
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$TT_POLICY_ARN" 2>/dev/null || true
  echo "Attached $TT_POLICY_NAME to $ROLE_NAME"
else
  echo "WARNING: Platform policy $TT_POLICY_NAME not found — handler code will lack DynamoDB/S3/Cognito/SES access." >&2
  echo "         Run launcher deploy_environment.py first to create it." >&2
fi

echo ""
echo "Done. Role ARN: arn:aws:iam::${AWS_ACCOUNT}:role/${ROLE_NAME}"
echo ""
