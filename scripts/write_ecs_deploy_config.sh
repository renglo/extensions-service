#!/usr/bin/env bash
set -euo pipefail

# Writes ecs_deploy_config.json for an extension: s3_bucket, cluster, task_definition,
# subnets and security_groups (auto-filled from default VPC when possible).
# Requires env: WORKSPACE_ROOT, EXTENSION_NAME, ECS_BUCKET, ECS_CLUSTER, ECS_TASK_FAMILY, AWS_REGION.

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
AWS_REGION="${AWS_REGION:-us-east-1}"
ECS_BUCKET="${ECS_BUCKET:?ECS_BUCKET required}"
ECS_CLUSTER="${ECS_CLUSTER:?ECS_CLUSTER required}"
ECS_TASK_FAMILY="${ECS_TASK_FAMILY:?ECS_TASK_FAMILY required}"

SERVICE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/installer/service"
mkdir -p "$SERVICE_DIR"
ECS_JSON="$SERVICE_DIR/ecs_deploy_config.json"

DEFAULT_VPC_ID=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION" 2>/dev/null || true)
SUBNETS_JSON="[]"
SECURITY_GROUPS_JSON="[]"
if [[ -n "$DEFAULT_VPC_ID" && "$DEFAULT_VPC_ID" != "None" ]]; then
  SUBNETS_JSON=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$DEFAULT_VPC_ID" --query 'Subnets[*].SubnetId' --output json --region "$AWS_REGION" 2>/dev/null || echo "[]")
  SG_ID=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$DEFAULT_VPC_ID" "Name=group-name,Values=default" --query 'SecurityGroups[0].GroupId' --output text --region "$AWS_REGION" 2>/dev/null || true)
  [[ -z "$SUBNETS_JSON" || "$SUBNETS_JSON" == "null" ]] && SUBNETS_JSON="[]"
  [[ -n "$SG_ID" && "$SG_ID" != "None" ]] && SECURITY_GROUPS_JSON="[\"$SG_ID\"]"
fi

cat > "$ECS_JSON" << ECSJSON
{
  "s3_bucket": "$ECS_BUCKET",
  "cluster": "$ECS_CLUSTER",
  "task_definition": "$ECS_TASK_FAMILY",
  "subnets": $SUBNETS_JSON,
  "security_groups": $SECURITY_GROUPS_JSON
}
ECSJSON
echo "Wrote $ECS_JSON"
[[ "$SUBNETS_JSON" == "[]" || "$SECURITY_GROUPS_JSON" == "[]" ]] && echo "No default VPC found; add subnets and security_groups to the file or set ECS_SUBNETS, ECS_SECURITY_GROUPS in env for invocations to work." >&2
