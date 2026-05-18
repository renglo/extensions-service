#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# Remove ECS EC2 capacity resources for one extension cluster:
# - disassociate capacity provider from cluster
# - delete capacity provider
# - scale ASG to 0 and delete it
# - delete launch template
# - delete instance profile and instance role
#
# Requires: EXTENSION_NAME, WORKSPACE_ROOT
# Optional: ECS_CLUSTER, AWS_REGION, AWS_PROFILE

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
ECS_CLUSTER="${ECS_CLUSTER:-${EXTENSION_NAME}-handlers}"

ECS_INSTANCE_ROLE_NAME="${EXTENSION_NAME}-handlers-ecs-instance"
ECS_INSTANCE_PROFILE_NAME="${EXTENSION_NAME}-handlers-ecs-instance-profile"
ECS_LT_NAME="${EXTENSION_NAME}-handlers-ecs-lt"
ECS_ASG_NAME="${EXTENSION_NAME}-handlers-ecs-asg"
ECS_CP_NAME="${EXTENSION_NAME}-handlers-ecs-cp"

echo "==> Disable ASG capacity (0/0/0) if group exists..."
if aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ECS_ASG_NAME" --query 'AutoScalingGroups[0].AutoScalingGroupName' --output text --region "$AWS_REGION" 2>/dev/null | grep -q "$ECS_ASG_NAME"; then
  aws autoscaling update-auto-scaling-group \
    --auto-scaling-group-name "$ECS_ASG_NAME" \
    --min-size 0 \
    --desired-capacity 0 \
    --max-size 0 \
    --region "$AWS_REGION" >/dev/null || true
fi

echo "==> Remove capacity provider from cluster (if present)..."
CLUSTER_STATUS=""
CLUSTER_STATUS=$(aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" \
  --query 'clusters[0].status' --output text 2>/dev/null || true)
if [[ "$CLUSTER_STATUS" != "ACTIVE" ]]; then
  echo "  Cluster not ACTIVE (${CLUSTER_STATUS:-missing}); skipping detach (idempotent rerun)."
elif aws ecs describe-capacity-providers --capacity-providers "$ECS_CP_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws ecs put-cluster-capacity-providers \
    --cluster "$ECS_CLUSTER" \
    --capacity-providers FARGATE FARGATE_SPOT \
    --default-capacity-provider-strategy capacityProvider=FARGATE,weight=1,base=0 \
    --region "$AWS_REGION" >/dev/null || true
fi

echo "==> Delete capacity provider..."
aws ecs delete-capacity-provider --capacity-provider "$ECS_CP_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || true

echo "==> Delete ASG..."
aws autoscaling delete-auto-scaling-group --auto-scaling-group-name "$ECS_ASG_NAME" --force-delete --region "$AWS_REGION" >/dev/null 2>&1 || true

echo "==> Delete launch template..."
aws ec2 delete-launch-template --launch-template-name "$ECS_LT_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || true

echo "==> Delete instance profile and role..."
aws iam remove-role-from-instance-profile --instance-profile-name "$ECS_INSTANCE_PROFILE_NAME" --role-name "$ECS_INSTANCE_ROLE_NAME" >/dev/null 2>&1 || true
aws iam delete-instance-profile --instance-profile-name "$ECS_INSTANCE_PROFILE_NAME" >/dev/null 2>&1 || true
aws iam detach-role-policy --role-name "$ECS_INSTANCE_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role" >/dev/null 2>&1 || true
aws iam detach-role-policy --role-name "$ECS_INSTANCE_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" >/dev/null 2>&1 || true
aws iam delete-role --role-name "$ECS_INSTANCE_ROLE_NAME" >/dev/null 2>&1 || true

echo ""
echo "Undeploy ECS EC2 capacity finished (best-effort cleanup)."
echo "Cluster kept: $ECS_CLUSTER"
echo ""
