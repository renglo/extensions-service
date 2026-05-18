#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# Provision base ECS infrastructure for an extension (admin-level permissions required):
#   - S3 results bucket + lifecycle policy
#   - ECR repository
#   - ECS cluster
#   - ECS task execution role (ECR pull + CloudWatch logs)
#   - ECS task role (S3, ECR, Lambda invoke — from ecs-task-role-policy.template.json)
#
# Requires: EXTENSION_NAME, WORKSPACE_ROOT
# Optional: AWS_REGION, AWS_PROFILE
#           ECS_RESULTS_BUCKET  — override bucket name (default: {ext}-handlers-ecs-{account_id})
#           ECS_CLUSTER         — override cluster name (default: {ext}-handlers)
#           ECS_VPC_ID          — VPC to discover subnets/SG from; uses default VPC if unset
#           PROVISION_OUTPUT_FILE — if set, writes resolved resource values as JSON to this path

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UTILS_DIR="$SERVICE_ROOT/utils"

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
[[ -z "$AWS_ACCOUNT" ]] && { echo "ERROR: Could not get AWS account ID" >&2; exit 1; }

ECS_BUCKET="${ECS_RESULTS_BUCKET:-${EXTENSION_NAME}-handlers-ecs-${AWS_ACCOUNT}}"
ECS_CLUSTER="${ECS_CLUSTER:-${EXTENSION_NAME}-handlers}"
ECR_REPO="${EXTENSION_NAME}-handlers-ecs"
EXECUTION_ROLE_NAME="${EXTENSION_NAME}-handlers-ecs-execution"
TASK_ROLE_NAME="${EXTENSION_NAME}-handlers-ecs-task"

echo "=========================================="
echo "Provision ECS Infra: $EXTENSION_NAME"
echo "Region: $AWS_REGION   Account: $AWS_ACCOUNT"
echo "Bucket: $ECS_BUCKET   Cluster: $ECS_CLUSTER"
[[ -n "${AWS_PROFILE:-}" ]] && echo "AWS Profile: $AWS_PROFILE"
echo "=========================================="
echo ""

# --- S3 results bucket ---
echo "==> S3 results bucket..."
if ! aws s3api head-bucket --bucket "$ECS_BUCKET" 2>/dev/null; then
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$ECS_BUCKET" --region "$AWS_REGION"
  else
    aws s3api create-bucket --bucket "$ECS_BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration "LocationConstraint=$AWS_REGION"
  fi
  echo "Created bucket $ECS_BUCKET"
else
  echo "Bucket $ECS_BUCKET already exists"
fi
LIFECYCLE_FILE="$UTILS_DIR/s3-lifecycle-payloads-results.json"
if [[ -f "$LIFECYCLE_FILE" ]]; then
  aws s3api put-bucket-lifecycle-configuration --bucket "$ECS_BUCKET" --lifecycle-configuration "file://$LIFECYCLE_FILE"
  echo "Lifecycle rule applied (1 day expiry)."
fi

# --- ECR repository ---
echo ""
echo "==> ECR repository..."
if ! aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION" >/dev/null
  echo "Created ECR repo $ECR_REPO"
else
  echo "ECR repo $ECR_REPO already exists"
fi
ECR_BASE_URI="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

# --- ECS cluster ---
echo ""
echo "==> ECS cluster..."
CLUSTER_STATUS=$(aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" \
  --query 'clusters[0].status' --output text 2>/dev/null || true)
if [[ "$CLUSTER_STATUS" == "ACTIVE" ]]; then
  echo "Cluster $ECS_CLUSTER already ACTIVE"
else
  aws ecs create-cluster --cluster-name "$ECS_CLUSTER" --region "$AWS_REGION" >/dev/null
  echo "Created cluster $ECS_CLUSTER"
fi

# --- IAM roles ---
echo ""
echo "==> IAM roles..."

# Task execution role (ECR pull + CloudWatch logs)
if ! aws iam get-role --role-name "$EXECUTION_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$EXECUTION_ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  aws iam attach-role-policy --role-name "$EXECUTION_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
  echo "Created execution role $EXECUTION_ROLE_NAME"
else
  echo "Execution role $EXECUTION_ROLE_NAME already exists"
fi
EXECUTION_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT}:role/${EXECUTION_ROLE_NAME}"

# Task role (S3 + ECR + Lambda invoke via template)
if ! aws iam get-role --role-name "$TASK_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$TASK_ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  echo "Created task role $TASK_ROLE_NAME"
else
  echo "Task role $TASK_ROLE_NAME already exists"
fi

TASK_POLICY_FILE=$(mktemp)
sed -e "s/{{ECS_BUCKET}}/$ECS_BUCKET/g" \
    -e "s/{{AWS_REGION}}/$AWS_REGION/g" \
    -e "s/{{AWS_ACCOUNT}}/$AWS_ACCOUNT/g" \
    -e "s/{{ECR_REPO}}/$ECR_REPO/g" \
    -e "s/{{EXTENSION_NAME}}/$EXTENSION_NAME/g" \
    "$UTILS_DIR/ecs-task-role-policy.template.json" > "$TASK_POLICY_FILE"
aws iam put-role-policy --role-name "$TASK_ROLE_NAME" --policy-name "ecs-handlers-s3-ecr" \
  --policy-document "file://$TASK_POLICY_FILE"
rm -f "$TASK_POLICY_FILE"
echo "Applied inline policy to $TASK_ROLE_NAME"

# Attach extension handlers policy to task role (same permissions as Lambda handlers role)
HANDLERS_POLICY_NAME="$(echo "${EXTENSION_NAME:0:1}" | tr '[:lower:]' '[:upper:]')${EXTENSION_NAME:1}HandlersPolicy"
HANDLERS_POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT}:policy/${HANDLERS_POLICY_NAME}"
if aws iam get-policy --policy-arn "$HANDLERS_POLICY_ARN" >/dev/null 2>&1; then
  aws iam attach-role-policy --role-name "$TASK_ROLE_NAME" --policy-arn "$HANDLERS_POLICY_ARN" 2>/dev/null || true
  echo "Attached $HANDLERS_POLICY_NAME to task role"
else
  echo "NOTE: Handlers policy $HANDLERS_POLICY_NAME not found yet — it will be attached on next apply after setup-iam." >&2
fi
TASK_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT}:role/${TASK_ROLE_NAME}"

# --- VPC networking (subnets + security groups, discovered from VPC) ---
echo ""
echo "==> VPC networking..."
RESOLVED_SUBNETS=""
RESOLVED_SGS=""

# Resolve which VPC to use: explicit ECS_VPC_ID or the account's default VPC
if [[ -n "${ECS_VPC_ID:-}" ]]; then
  VPC_ID="$ECS_VPC_ID"
  echo "Using provided VPC: $VPC_ID"
else
  VPC_ID="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true \
    --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION" 2>/dev/null || true)"
  if [[ -z "$VPC_ID" || "$VPC_ID" == "None" ]]; then
    echo "WARNING: No default VPC found in $AWS_REGION. Pass --vpc vpc-xxxx to provision-infra apply." >&2
    VPC_ID=""
  else
    echo "Using default VPC: $VPC_ID"
  fi
fi

if [[ -n "$VPC_ID" ]]; then
  DISCOVERED_SUBNETS="$(aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'Subnets[*].SubnetId' --output text --region "$AWS_REGION" 2>/dev/null || true)"
  RESOLVED_SUBNETS="$(echo "$DISCOVERED_SUBNETS" | tr -s '[:space:]' ',' | sed 's/^,*//; s/,*$//')"
  [[ -n "$RESOLVED_SUBNETS" ]] && echo "Discovered subnets: $RESOLVED_SUBNETS"

  RESOLVED_SGS="$(aws ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=default" \
    --query 'SecurityGroups[0].GroupId' --output text --region "$AWS_REGION" 2>/dev/null || true)"
  [[ "$RESOLVED_SGS" == "None" ]] && RESOLVED_SGS=""
  [[ -n "$RESOLVED_SGS" ]] && echo "Discovered default security group: $RESOLVED_SGS"
fi

# --- Write output JSON for Python to consume ---
if [[ -n "${PROVISION_OUTPUT_FILE:-}" ]]; then
  SUBNETS_VAL="$RESOLVED_SUBNETS"
  SGS_VAL="$RESOLVED_SGS"
  python3 - <<PY
import json, os
subnets_raw = os.environ.get("_SUBNETS_VAL", "")
sgs_raw = os.environ.get("_SGS_VAL", "")
data = {
    "aws_account": "$AWS_ACCOUNT",
    "aws_region": "$AWS_REGION",
    "ecr_repo": "$ECR_REPO",
    "ecr_base_uri": "$ECR_BASE_URI",
    "ecs_bucket": "$ECS_BUCKET",
    "ecs_cluster": "$ECS_CLUSTER",
    "execution_role_arn": "$EXECUTION_ROLE_ARN",
    "task_role_arn": "$TASK_ROLE_ARN",
    "subnets": [s.strip() for s in "$RESOLVED_SUBNETS".split(",") if s.strip()],
    "security_groups": [s.strip() for s in "$RESOLVED_SGS".split(",") if s.strip()],
}
with open("$PROVISION_OUTPUT_FILE", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
PY
  echo "Provision output written to $PROVISION_OUTPUT_FILE"
fi

echo ""
echo "ECS infra provisioned:"
echo "  ECR:              $ECR_BASE_URI"
echo "  S3:               $ECS_BUCKET"
echo "  ECS cluster:      $ECS_CLUSTER"
echo "  Execution role:   $EXECUTION_ROLE_ARN"
echo "  Task role:        $TASK_ROLE_ARN"
[[ -n "$RESOLVED_SUBNETS" ]] && echo "  Subnets:          $RESOLVED_SUBNETS"
[[ -n "$RESOLVED_SGS" ]] && echo "  Security groups:  $RESOLVED_SGS"
echo ""
