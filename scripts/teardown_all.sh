#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# DESTRUCTIVE: Remove ALL AWS resources created by provision-infra apply for one extension.
# Handlers GitHub OIDC IAM roles/policies are removed first by provision_infra.cmd_teardown (Python),
# then this script continues with:
# Resources removed (in dependency order):
#   EC2 capacity (ASG, launch template, capacity provider, instance role/profile)
#   ECS task definitions (all revisions deregistered)
#   ECS cluster
#   ECR repository (all images)
#   S3 results bucket (all objects, then bucket)
#   IAM ECS roles (task + execution)
#   IAM Lambda role + managed policy
#   CloudWatch log groups (ECS + optional Lambda), unless TEARDOWN_KEEP_LOGS=1
#
# Requires: EXTENSION_NAME, WORKSPACE_ROOT
# Optional: AWS_REGION, AWS_PROFILE, ECS_CLUSTER, ECS_RESULTS_BUCKET
#           TEARDOWN_KEEP_LOGS=1 — skip deleting CloudWatch log groups
#           LAMBDA_LOG_GROUP_NAME — e.g. /aws/lambda/my-fn (deleted when logs not kept)
#           TEARDOWN_PYTHON — python with boto3 (auto-set by run.py provision-infra teardown)

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
[[ -z "$AWS_ACCOUNT" ]] && { echo "ERROR: Could not get AWS account ID" >&2; exit 1; }

ECS_CLUSTER="${ECS_CLUSTER:-${EXTENSION_NAME}-handlers}"
ECS_BUCKET="${ECS_RESULTS_BUCKET:-${EXTENSION_NAME}-handlers-ecs-${AWS_ACCOUNT}}"
ECR_REPO="${EXTENSION_NAME}-handlers-ecs"
ECS_TASK_FAMILY="${EXTENSION_NAME}-handlers-ecs"
EXECUTION_ROLE_NAME="${EXTENSION_NAME}-handlers-ecs-execution"
TASK_ROLE_NAME="${EXTENSION_NAME}-handlers-ecs-task"
LAMBDA_ROLE_NAME="${EXTENSION_NAME}-handlers-role"
POLICY_NAME="$(echo "${EXTENSION_NAME:0:1}" | tr '[:lower:]' '[:upper:]')${EXTENSION_NAME:1}HandlersPolicy"
POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT}:policy/${POLICY_NAME}"

# Inline S3 cleanup uses boto3. TEARDOWN_PYTHON is set by provision_infra.cmd_teardown (run.py venv).
_resolve_teardown_python() {
  if [[ -n "${TEARDOWN_PYTHON:-}" && -f "$TEARDOWN_PYTHON" ]]; then
    printf '%s\n' "$TEARDOWN_PYTHON"
    return
  fi
  local cand
  for cand in "${WORKSPACE_ROOT}/extensions-service/venv/bin/python" \
              "${WORKSPACE_ROOT}/extensions-service/venv/Scripts/python.exe"; do
    if [[ -f "$cand" ]]; then
      printf '%s\n' "$cand"
      return
    fi
  done
  printf '%s\n' python3
}
TEARDOWN_PY="$(_resolve_teardown_python)"

# After EC2 capacity teardown, ECS may still report UpdateInProgress on cluster attachments.
# Initial delay + exponential backoff (cap 120s) on DeleteCluster.
_delete_ecs_cluster_with_retry() {
  local cluster="$1" region="$2"
  local max_attempts=12 attempt=1 wait_sec=15
  local output

  echo "  Waiting 30s for cluster capacity updates to settle before delete..."
  sleep 30

  while (( attempt <= max_attempts )); do
    if output=$(aws ecs delete-cluster --cluster "$cluster" --region "$region" 2>&1); then
      echo "  Deleted cluster $cluster"
      return 0
    fi
    if [[ "$output" == *UpdateInProgressException* ]]; then
      echo "  Cluster busy (UpdateInProgress); attempt $attempt/$max_attempts, sleeping ${wait_sec}s..."
      sleep "$wait_sec"
      wait_sec=$((wait_sec * 2))
      (( wait_sec > 120 )) && wait_sec=120
      attempt=$((attempt + 1))
      continue
    fi
    if [[ "$output" == *ResourceNotFoundException* || "$output" == *ClusterNotFoundException* ]]; then
      echo "  Cluster $cluster not found (already deleted)."
      return 0
    fi
    echo "$output" >&2
    return 1
  done

  echo "ERROR: delete-cluster still busy after $max_attempts attempts: $cluster" >&2
  return 1
}

echo "=========================================="
echo "TEARDOWN ALL: $EXTENSION_NAME"
echo "Region: $AWS_REGION   Account: $AWS_ACCOUNT"
echo "=========================================="
echo ""
echo "Resources to remove:"
echo "  ECS cluster:    $ECS_CLUSTER"
echo "  ECR repo:       $ECR_REPO"
echo "  S3 bucket:      $ECS_BUCKET"
echo "  IAM roles:      $LAMBDA_ROLE_NAME, $EXECUTION_ROLE_NAME, $TASK_ROLE_NAME"
echo "  IAM policy:     $POLICY_NAME"
if [[ "${TEARDOWN_KEEP_LOGS:-}" == "1" || "${TEARDOWN_KEEP_LOGS:-}" == "true" ]]; then
  echo "  CloudWatch:     KEPT (TEARDOWN_KEEP_LOGS / --keep-logs)"
else
  echo "  CloudWatch:     /ecs/$ECS_TASK_FAMILY${LAMBDA_LOG_GROUP_NAME:+ + $LAMBDA_LOG_GROUP_NAME}"
fi
echo ""

# ── Step 1: EC2 capacity (reuse existing script) ──────────────────────────────
echo "==> [1/8] EC2 capacity (ASG, launch template, capacity provider, instance role)..."
"$SCRIPT_DIR/undeploy_ecs_capacity.sh" || true
echo ""

# ── Step 2: ECS task definitions ─────────────────────────────────────────────
echo "==> [2/8] ECS task definitions..."
TASK_DEF_ARNS=$(aws ecs list-task-definitions \
  --family-prefix "$ECS_TASK_FAMILY" \
  --region "$AWS_REGION" \
  --query 'taskDefinitionArns[]' \
  --output text 2>/dev/null || true)
if [[ -n "$TASK_DEF_ARNS" && "$TASK_DEF_ARNS" != "None" ]]; then
  for arn in $TASK_DEF_ARNS; do
    aws ecs deregister-task-definition --task-definition "$arn" --region "$AWS_REGION" >/dev/null 2>&1 || true
    echo "  Deregistered $arn"
  done
else
  echo "  No task definitions found."
fi

# ── Step 3: ECS cluster ───────────────────────────────────────────────────────
echo ""
echo "==> [3/8] ECS cluster..."
CLUSTER_STATUS=$(aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" \
  --query 'clusters[0].status' --output text 2>/dev/null || true)
if [[ "$CLUSTER_STATUS" == "ACTIVE" ]]; then
  _delete_ecs_cluster_with_retry "$ECS_CLUSTER" "$AWS_REGION"
else
  echo "  Cluster $ECS_CLUSTER not found or already deleted."
fi

# ── Step 4: ECR repository ────────────────────────────────────────────────────
echo ""
echo "==> [4/8] ECR repository..."
if aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1; then
  IMAGE_IDS=$(aws ecr list-images --repository-name "$ECR_REPO" --region "$AWS_REGION" \
    --query 'imageIds[]' --output json 2>/dev/null || echo "[]")
  if [[ "$IMAGE_IDS" != "[]" && -n "$IMAGE_IDS" ]]; then
    aws ecr batch-delete-image --repository-name "$ECR_REPO" --region "$AWS_REGION" \
      --image-ids "$IMAGE_IDS" >/dev/null 2>&1 || true
    echo "  Deleted all images from $ECR_REPO"
  fi
  aws ecr delete-repository --repository-name "$ECR_REPO" --region "$AWS_REGION" --force >/dev/null
  echo "  Deleted ECR repo $ECR_REPO"
else
  echo "  ECR repo $ECR_REPO not found."
fi

# ── Step 5: S3 bucket ─────────────────────────────────────────────────────────
echo ""
echo "==> [5/8] S3 bucket..."
if aws s3api head-bucket --bucket "$ECS_BUCKET" 2>/dev/null; then
  aws s3 rm "s3://$ECS_BUCKET" --recursive --region "$AWS_REGION" 2>/dev/null || true
  "$TEARDOWN_PY" - <<PY
import boto3, os
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
bucket = "$ECS_BUCKET"
try:
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                   for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
except Exception as e:
    print(f"  (version cleanup skipped: {e})")
PY
  aws s3api delete-bucket --bucket "$ECS_BUCKET" --region "$AWS_REGION" >/dev/null
  echo "  Deleted bucket $ECS_BUCKET"
else
  echo "  Bucket $ECS_BUCKET not found."
fi

# ── Step 6: IAM ECS roles ─────────────────────────────────────────────────────
echo ""
echo "==> [6/8] IAM ECS roles..."

if aws iam get-role --role-name "$EXECUTION_ROLE_NAME" >/dev/null 2>&1; then
  aws iam detach-role-policy --role-name "$EXECUTION_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy" >/dev/null 2>&1 || true
  aws iam delete-role --role-name "$EXECUTION_ROLE_NAME" >/dev/null
  echo "  Deleted role $EXECUTION_ROLE_NAME"
else
  echo "  Role $EXECUTION_ROLE_NAME not found."
fi

if aws iam get-role --role-name "$TASK_ROLE_NAME" >/dev/null 2>&1; then
  aws iam delete-role-policy --role-name "$TASK_ROLE_NAME" \
    --policy-name "ecs-handlers-s3-ecr" >/dev/null 2>&1 || true
  aws iam detach-role-policy --role-name "$TASK_ROLE_NAME" \
    --policy-arn "$POLICY_ARN" >/dev/null 2>&1 || true
  aws iam delete-role --role-name "$TASK_ROLE_NAME" >/dev/null
  echo "  Deleted role $TASK_ROLE_NAME"
else
  echo "  Role $TASK_ROLE_NAME not found."
fi

# ── Step 7: Lambda role + managed policy ─────────────────────────────────────
echo ""
echo "==> [7/8] Lambda role and managed policy..."

if aws iam get-role --role-name "$LAMBDA_ROLE_NAME" >/dev/null 2>&1; then
  ATTACHED=$(aws iam list-attached-role-policies --role-name "$LAMBDA_ROLE_NAME" \
    --query 'AttachedPolicies[*].PolicyArn' --output text 2>/dev/null || true)
  for parn in $ATTACHED; do
    aws iam detach-role-policy --role-name "$LAMBDA_ROLE_NAME" --policy-arn "$parn" >/dev/null 2>&1 || true
  done
  aws iam delete-role --role-name "$LAMBDA_ROLE_NAME" >/dev/null
  echo "  Deleted role $LAMBDA_ROLE_NAME"
else
  echo "  Role $LAMBDA_ROLE_NAME not found."
fi

if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  VERSIONS=$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
    --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text 2>/dev/null || true)
  for vid in $VERSIONS; do
    aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$vid" >/dev/null 2>&1 || true
  done
  aws iam delete-policy --policy-arn "$POLICY_ARN" >/dev/null
  echo "  Deleted policy $POLICY_NAME"
else
  echo "  Policy $POLICY_NAME not found."
fi

# ── Step 8: CloudWatch Logs (optional retention via TEARDOWN_KEEP_LOGS) ───────
echo ""
echo "==> [8/8] CloudWatch Logs..."
ECS_LOG_GROUP="/ecs/${ECS_TASK_FAMILY}"
if [[ "${TEARDOWN_KEEP_LOGS:-}" == "1" || "${TEARDOWN_KEEP_LOGS:-}" == "true" ]]; then
  echo "  Skipped (TEARDOWN_KEEP_LOGS). Preserved: $ECS_LOG_GROUP"
  [[ -n "${LAMBDA_LOG_GROUP_NAME:-}" ]] && echo "  Preserved: $LAMBDA_LOG_GROUP_NAME"
else
  if aws logs describe-log-groups --log-group-name-prefix "$ECS_LOG_GROUP" --region "$AWS_REGION" \
    --query "logGroups[?logGroupName=='$ECS_LOG_GROUP'].logGroupName" --output text 2>/dev/null | grep -q .; then
    aws logs delete-log-group --log-group-name "$ECS_LOG_GROUP" --region "$AWS_REGION" >/dev/null 2>&1 || true
    echo "  Deleted log group $ECS_LOG_GROUP"
  else
    echo "  Log group $ECS_LOG_GROUP not found."
  fi
  if [[ -n "${LAMBDA_LOG_GROUP_NAME:-}" ]]; then
    if aws logs describe-log-groups --log-group-name-prefix "$LAMBDA_LOG_GROUP_NAME" --region "$AWS_REGION" \
      --query "logGroups[?logGroupName=='$LAMBDA_LOG_GROUP_NAME'].logGroupName" --output text 2>/dev/null | grep -q .; then
      aws logs delete-log-group --log-group-name "$LAMBDA_LOG_GROUP_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || true
      echo "  Deleted log group $LAMBDA_LOG_GROUP_NAME"
    else
      echo "  Log group $LAMBDA_LOG_GROUP_NAME not found."
    fi
  fi
fi

echo ""
echo "Teardown complete for $EXTENSION_NAME."
echo ""
