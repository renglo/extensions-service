#!/usr/bin/env bash
set -euo pipefail

# Deploy extension handlers to ECS (Fargate): S3 bucket + lifecycle, ECR, cluster, task definition.
# Requires: EXTENSION_NAME, WORKSPACE_ROOT. Image must exist (run build --large first).
# Optional env: ECS_RESULTS_BUCKET, ECS_CLUSTER, ECS_TASK_DEFINITION, AWS_REGION, AWS_PROFILE.

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UTILS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/utils"
PACKAGE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/package"
DOCKER_IMAGE="${EXTENSION_NAME}-ecs-builder:latest"

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
[[ -z "$AWS_ACCOUNT" ]] && { echo "ERROR: Could not get AWS account" >&2; exit 1; }

ECS_BUCKET="${ECS_RESULTS_BUCKET:-${EXTENSION_NAME}-handlers-ecs-${AWS_ACCOUNT}}"
ECS_CLUSTER="${ECS_CLUSTER:-${EXTENSION_NAME}-handlers}"
ECS_TASK_FAMILY="${ECS_TASK_DEFINITION:-${EXTENSION_NAME}-handlers-ecs}"
ECR_REPO="${EXTENSION_NAME}-handlers-ecs"
EXECUTION_ROLE_NAME="${EXTENSION_NAME}-handlers-ecs-execution"
TASK_ROLE_NAME="${EXTENSION_NAME}-handlers-ecs-task"

echo "=========================================="
echo "ECS Deployment: $EXTENSION_NAME"
echo "Bucket: $ECS_BUCKET  Cluster: $ECS_CLUSTER  Task: $ECS_TASK_FAMILY"
[[ -n "${AWS_PROFILE:-}" ]] && echo "AWS Profile: $AWS_PROFILE"
echo "=========================================="
echo ""

if ! docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
  echo "ERROR: Docker image $DOCKER_IMAGE not found. Run: python3 dev/extensions-service/run.py $EXTENSION_NAME build --large" >&2
  exit 1
fi

echo "==> S3 bucket..."
if ! aws s3api head-bucket --bucket "$ECS_BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$ECS_BUCKET" --region "$AWS_REGION" \
    $([[ "$AWS_REGION" != "us-east-1" ]] && echo "--create-bucket-configuration LocationConstraint=$AWS_REGION" || true)
  echo "Created bucket $ECS_BUCKET"
fi
# Lifecycle: expire payloads/ and results/ after 1 day
LIFECYCLE_FILE="$UTILS_DIR/s3-lifecycle-payloads-results.json"
aws s3api put-bucket-lifecycle-configuration --bucket "$ECS_BUCKET" --lifecycle-configuration "file://$LIFECYCLE_FILE"
echo "Lifecycle rule set (1 day)."

echo "==> ECR repository..."
if ! aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" 2>/dev/null; then
  aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION"
  echo "Created ECR repo $ECR_REPO"
fi
ECR_URI="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:latest"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker tag "$DOCKER_IMAGE" "$ECR_URI"
docker push "$ECR_URI"
echo "Pushed $ECR_URI"

echo "==> ECS cluster..."
if ! aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" --query 'clusters[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs create-cluster --cluster-name "$ECS_CLUSTER" --region "$AWS_REGION"
  echo "Created cluster $ECS_CLUSTER"
fi

echo "==> IAM roles..."
# Execution role (for ECR pull and CloudWatch logs)
if ! aws iam get-role --role-name "$EXECUTION_ROLE_NAME" 2>/dev/null; then
  aws iam create-role --role-name "$EXECUTION_ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  aws iam attach-role-policy --role-name "$EXECUTION_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
  echo "Created execution role $EXECUTION_ROLE_NAME"
fi
EXECUTION_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT}:role/${EXECUTION_ROLE_NAME}"

# Task role (for S3, ECR, and Lambda invoke)
if ! aws iam get-role --role-name "$TASK_ROLE_NAME" 2>/dev/null; then
  aws iam create-role --role-name "$TASK_ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  echo "Created task role $TASK_ROLE_NAME"
fi
# Always apply/update inline policy from template (safe: replaces only this policy, no accumulation)
TASK_POLICY=$(mktemp)
sed -e "s/{{ECS_BUCKET}}/$ECS_BUCKET/g" \
    -e "s/{{AWS_REGION}}/$AWS_REGION/g" \
    -e "s/{{AWS_ACCOUNT}}/$AWS_ACCOUNT/g" \
    -e "s/{{ECR_REPO}}/$ECR_REPO/g" \
    -e "s/{{EXTENSION_NAME}}/$EXTENSION_NAME/g" \
    "$UTILS_DIR/ecs-task-role-policy.template.json" > "$TASK_POLICY"
aws iam put-role-policy --role-name "$TASK_ROLE_NAME" --policy-name "ecs-handlers-s3-ecr" --policy-document "file://$TASK_POLICY"
rm -f "$TASK_POLICY"
TASK_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT}:role/${TASK_ROLE_NAME}"

# Attach extension handlers policy to task role (same as Lambda: S3, IAM, etc. for handler operations)
HANDLERS_POLICY_NAME="$(echo "${EXTENSION_NAME:0:1}" | tr '[:lower:]' '[:upper:]')${EXTENSION_NAME:1}HandlersPolicy"
HANDLERS_POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT}:policy/${HANDLERS_POLICY_NAME}"
if aws iam get-policy --policy-arn "$HANDLERS_POLICY_ARN" >/dev/null 2>&1; then
  aws iam attach-role-policy --role-name "$TASK_ROLE_NAME" --policy-arn "$HANDLERS_POLICY_ARN"
  echo "Attached $HANDLERS_POLICY_NAME to task role (same as Lambda handlers)"
else
  echo "WARNING: Policy $HANDLERS_POLICY_NAME not found. ECS task will only have ECS bucket + ECR access." >&2
  echo "         Run: python3 dev/extensions-service/run.py $EXTENSION_NAME setup-iam" >&2
fi

echo "==> Task definition..."
TASK_DEF=$(mktemp)
sed -e "s|{{ECS_TASK_FAMILY}}|$ECS_TASK_FAMILY|g" \
    -e "s|{{EXECUTION_ROLE_ARN}}|$EXECUTION_ROLE_ARN|g" \
    -e "s|{{TASK_ROLE_ARN}}|$TASK_ROLE_ARN|g" \
    -e "s|{{ECR_URI}}|$ECR_URI|g" \
    -e "s|{{AWS_REGION}}|$AWS_REGION|g" \
    "$UTILS_DIR/ecs-task-definition.template.json" > "$TASK_DEF"
aws logs create-log-group --log-group-name "/ecs/${ECS_TASK_FAMILY}" --region "$AWS_REGION" 2>/dev/null || true
aws ecs register-task-definition --cli-input-json "file://$TASK_DEF" --region "$AWS_REGION" >/dev/null
rm -f "$TASK_DEF"
echo "Registered task definition $ECS_TASK_FAMILY"

echo "==> ECS deploy config..."
export ECS_BUCKET ECS_CLUSTER ECS_TASK_FAMILY AWS_REGION
"$SCRIPT_DIR/write_ecs_deploy_config.sh"

echo ""
echo "Deploy complete. ECS config written to extensions/$EXTENSION_NAME/installer/service/ecs_deploy_config.json"
echo ""
