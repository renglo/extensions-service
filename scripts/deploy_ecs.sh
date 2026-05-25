#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# Deploy extension handlers to ECS (Fargate and/or EC2 per extensions/<name>/installer/ecs_profile.json).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT. Image must exist (run build --large first).
# Optional env: ECS_RESULTS_BUCKET, ECS_CLUSTER, ECS_TASK_DEFINITION, AWS_REGION, AWS_PROFILE.

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UTILS_DIR="$SERVICE_ROOT/utils"
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
TT_POLICY_NAME="${EXTENSION_NAME}_tt_policy"
TT_POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT}:policy/${TT_POLICY_NAME}"

echo "=========================================="
echo "ECS Deployment: $EXTENSION_NAME"
echo "Bucket: $ECS_BUCKET  Cluster: $ECS_CLUSTER  Task: $ECS_TASK_FAMILY"
[[ -n "${AWS_PROFILE:-}" ]] && echo "AWS Profile: $AWS_PROFILE"
echo "=========================================="
echo ""

PROFILE_ENV=$(mktemp)
if [[ -n "${ECS_PROFILE_FILE:-}" && -f "${ECS_PROFILE_FILE}" ]]; then
  ECS_PROFILE_FILE="$ECS_PROFILE_FILE" python3 -c "
import json, os
data = json.load(open(os.environ['ECS_PROFILE_FILE'], encoding='utf-8'))
print(f\"ECS_PROFILE_LAUNCH_TYPE={data.get('launch_type','fargate')}\")
print(f\"ECS_PROFILE_NETWORK_MODE={data.get('network_mode','awsvpc')}\")
print(f\"ECS_PROFILE_TASK_CPU={data.get('task_cpu',1024)}\")
print(f\"ECS_PROFILE_TASK_MEMORY={data.get('task_memory',4096)}\")
" > "$PROFILE_ENV"
else
  python3 "$SERVICE_ROOT/ecs_profile.py" export-for-deploy "$WORKSPACE_ROOT" "$EXTENSION_NAME" > "$PROFILE_ENV"
fi
# shellcheck disable=SC1090
source "$PROFILE_ENV"
rm -f "$PROFILE_ENV"

ECS_PROFILE_LAUNCH_TYPE="${ECS_PROFILE_LAUNCH_TYPE:-fargate}"
ECS_PROFILE_NETWORK_MODE="${ECS_PROFILE_NETWORK_MODE:-awsvpc}"
ECS_PROFILE_TASK_CPU="${ECS_PROFILE_TASK_CPU:-1024}"
ECS_PROFILE_TASK_MEMORY="${ECS_PROFILE_TASK_MEMORY:-4096}"

echo "ECS profile: launch_type=$ECS_PROFILE_LAUNCH_TYPE network_mode=$ECS_PROFILE_NETWORK_MODE cpu=$ECS_PROFILE_TASK_CPU memory=$ECS_PROFILE_TASK_MEMORY"
echo ""

TASK_DEF_TEMPLATE="$UTILS_DIR/ecs-task-definition.template.json"
if [[ "$ECS_PROFILE_LAUNCH_TYPE" == "ec2" ]]; then
  case "$ECS_PROFILE_NETWORK_MODE" in
    bridge) TASK_DEF_TEMPLATE="$UTILS_DIR/ecs-task-definition-ec2-bridge.template.json" ;;
    host) TASK_DEF_TEMPLATE="$UTILS_DIR/ecs-task-definition-ec2-host.template.json" ;;
    awsvpc) TASK_DEF_TEMPLATE="$UTILS_DIR/ecs-task-definition-ec2-awsvpc.template.json" ;;
    *) echo "ERROR: Unknown ECS network_mode $ECS_PROFILE_NETWORK_MODE" >&2; exit 1 ;;
  esac
fi
echo "Task definition template: $(basename "$TASK_DEF_TEMPLATE")"
echo ""

if ! docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
  echo "ERROR: Docker image $DOCKER_IMAGE not found. Run: python3 run.py $EXTENSION_NAME build --large" >&2
  exit 1
fi

echo "==> S3 bucket..."
if ! aws s3api head-bucket --bucket "$ECS_BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$ECS_BUCKET" --region "$AWS_REGION" \
    $([[ "$AWS_REGION" != "us-east-1" ]] && echo "--create-bucket-configuration LocationConstraint=$AWS_REGION" || true)
  echo "Created bucket $ECS_BUCKET"
fi
reglo_tag_s3_bucket "$ECS_BUCKET"
# Lifecycle: expire payloads/ and results/ after 1 day
LIFECYCLE_FILE="$UTILS_DIR/s3-lifecycle-payloads-results.json"
aws s3api put-bucket-lifecycle-configuration --bucket "$ECS_BUCKET" --lifecycle-configuration "file://$LIFECYCLE_FILE"
echo "Lifecycle rule set (1 day)."

echo "==> ECR repository..."
if ! aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" 2>/dev/null; then
  aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION"
  echo "Created ECR repo $ECR_REPO"
fi
reglo_tag_ecr_repository "$ECR_REPO" "$AWS_REGION"
ECR_URI="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:latest"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker tag "$DOCKER_IMAGE" "$ECR_URI"
docker push "$ECR_URI"
echo "Pushed $ECR_URI"

echo "==> ECS cluster..."
if ! aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" --query 'clusters[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs create-cluster --cluster-name "$ECS_CLUSTER" --region "$AWS_REGION" \
    --tags "key=Description,value=${REGLO_DEPLOYMENT_DESCRIPTION}"
  echo "Created cluster $ECS_CLUSTER"
fi
reglo_tag_ecs_cluster "$ECS_CLUSTER" "$AWS_REGION"

echo "==> IAM roles..."
# Execution role (for ECR pull and CloudWatch logs)
if ! aws iam get-role --role-name "$EXECUTION_ROLE_NAME" 2>/dev/null; then
  reglo_create_iam_role "$EXECUTION_ROLE_NAME" \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  aws iam attach-role-policy --role-name "$EXECUTION_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
  echo "Created execution role $EXECUTION_ROLE_NAME"
else
  reglo_ensure_iam_role_description "$EXECUTION_ROLE_NAME"
fi
EXECUTION_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT}:role/${EXECUTION_ROLE_NAME}"

# Task role (for S3, ECR, and Lambda invoke)
if ! aws iam get-role --role-name "$TASK_ROLE_NAME" 2>/dev/null; then
  reglo_create_iam_role "$TASK_ROLE_NAME" \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  echo "Created task role $TASK_ROLE_NAME"
else
  reglo_ensure_iam_role_description "$TASK_ROLE_NAME"
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
  echo "         Run: python3 run.py $EXTENSION_NAME setup-iam" >&2
fi

# Attach the platform runtime policy so ECS handler code can access DynamoDB, S3, Cognito, SES, etc.
if aws iam get-policy --policy-arn "$TT_POLICY_ARN" >/dev/null 2>&1; then
  aws iam attach-role-policy --role-name "$TASK_ROLE_NAME" --policy-arn "$TT_POLICY_ARN" 2>/dev/null || true
  echo "Attached $TT_POLICY_NAME to $TASK_ROLE_NAME"
else
  echo "WARNING: Platform policy $TT_POLICY_NAME not found — ECS handler code will lack DynamoDB/S3/Cognito/SES access." >&2
  echo "         Run launcher deploy_environment.py first to create it." >&2
fi

echo "==> Task definition..."
TASK_DEF=$(mktemp)
sed -e "s|{{ECS_TASK_FAMILY}}|$ECS_TASK_FAMILY|g" \
    -e "s|{{EXECUTION_ROLE_ARN}}|$EXECUTION_ROLE_ARN|g" \
    -e "s|{{TASK_ROLE_ARN}}|$TASK_ROLE_ARN|g" \
    -e "s|{{ECR_URI}}|$ECR_URI|g" \
    -e "s|{{AWS_REGION}}|$AWS_REGION|g" \
    -e "s|{{TASK_CPU}}|$ECS_PROFILE_TASK_CPU|g" \
    -e "s|{{TASK_MEMORY}}|$ECS_PROFILE_TASK_MEMORY|g" \
    "$TASK_DEF_TEMPLATE" > "$TASK_DEF"
# Task env: VARS+SECRETS from DEPLOY_INPUT_FILE, or ECS_ENV_FILE
GENERATED_ECS_ENV_FILE=""
ECS_ENV_FILE_MERGE=""
if [[ -n "${DEPLOY_INPUT_FILE:-}" && -f "${DEPLOY_INPUT_FILE}" ]]; then
  GENERATED_ECS_ENV_FILE=$(mktemp)
  python3 "$SERVICE_ROOT/deploy_input.py" export-runtime-env "$DEPLOY_INPUT_FILE" \
    -o "$GENERATED_ECS_ENV_FILE" || exit 1
  ECS_ENV_FILE_MERGE="$GENERATED_ECS_ENV_FILE"
elif [[ -n "${ECS_ENV_FILE:-}" && -f "${ECS_ENV_FILE}" ]]; then
  ECS_ENV_FILE_MERGE="$ECS_ENV_FILE"
fi
if [[ -n "$ECS_ENV_FILE_MERGE" && -f "$ECS_ENV_FILE_MERGE" ]]; then
  TASK_DEF_JSON="$TASK_DEF" ECS_ENV_FILE="$ECS_ENV_FILE_MERGE" python3 -c "
import json
import os
task_def_path = os.environ['TASK_DEF_JSON']
env_file = os.environ['ECS_ENV_FILE']
with open(task_def_path) as f:
    td = json.load(f)
with open(env_file) as f:
    env = json.load(f)
td['containerDefinitions'][0]['environment'] = [{'name': k, 'value': str(v)} for k, v in env.items()]
with open(task_def_path, 'w') as f:
    json.dump(td, f, indent=2)
"
  echo "Merged container environment from deploy input / ECS_ENV_FILE into task definition"
fi
ensure_cloudwatch_log_group "/ecs/${ECS_TASK_FAMILY}" "$AWS_REGION"
aws ecs register-task-definition --cli-input-json "file://$TASK_DEF" --region "$AWS_REGION" >/dev/null
rm -f "$TASK_DEF"
echo "Registered task definition $ECS_TASK_FAMILY"

echo "==> ECS deploy config..."
export ECS_BUCKET ECS_CLUSTER ECS_TASK_FAMILY AWS_REGION
export ECS_LAUNCH_TYPE="$ECS_PROFILE_LAUNCH_TYPE"
export ECS_NETWORK_MODE="$ECS_PROFILE_NETWORK_MODE"
"$SCRIPT_DIR/write_ecs_deploy_config.sh"

echo ""
echo "Deploy complete. ECS config written to extensions/$EXTENSION_NAME/installer/service/ecs_deploy_config.json"
echo ""
[[ -n "$GENERATED_ECS_ENV_FILE" && -f "$GENERATED_ECS_ENV_FILE" ]] && rm -f "$GENERATED_ECS_ENV_FILE"
