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

ECS_LAUNCH_TYPE="${ECS_LAUNCH_TYPE:-fargate}"
ECS_NETWORK_MODE="${ECS_NETWORK_MODE:-awsvpc}"

SERVICE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/installer/service"
mkdir -p "$SERVICE_DIR"
ECS_JSON="$SERVICE_DIR/ecs_deploy_config.json"

SUBNETS_JSON="[]"
SECURITY_GROUPS_JSON="[]"

NEEDS_VPC_NET=1
if [[ "$ECS_LAUNCH_TYPE" == "ec2" && ( "$ECS_NETWORK_MODE" == "bridge" || "$ECS_NETWORK_MODE" == "host" ) ]]; then
  NEEDS_VPC_NET=0
fi

if [[ "$NEEDS_VPC_NET" -eq 1 ]]; then
  DEFAULT_VPC_ID=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION" 2>/dev/null || true)
  if [[ -n "$DEFAULT_VPC_ID" && "$DEFAULT_VPC_ID" != "None" ]]; then
    SUBNETS_JSON=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$DEFAULT_VPC_ID" --query 'Subnets[*].SubnetId' --output json --region "$AWS_REGION" 2>/dev/null || echo "[]")
    SG_ID=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$DEFAULT_VPC_ID" "Name=group-name,Values=default" --query 'SecurityGroups[0].GroupId' --output text --region "$AWS_REGION" 2>/dev/null || true)
    [[ -z "$SUBNETS_JSON" || "$SUBNETS_JSON" == "null" ]] && SUBNETS_JSON="[]"
    [[ -n "$SG_ID" && "$SG_ID" != "None" ]] && SECURITY_GROUPS_JSON="[\"$SG_ID\"]"
  fi
fi

cat > "$ECS_JSON" << ECSJSON
{
  "s3_bucket": "$ECS_BUCKET",
  "cluster": "$ECS_CLUSTER",
  "task_definition": "$ECS_TASK_FAMILY",
  "launch_type": "$ECS_LAUNCH_TYPE",
  "network_mode": "$ECS_NETWORK_MODE",
  "subnets": $SUBNETS_JSON,
  "security_groups": $SECURITY_GROUPS_JSON
}
ECSJSON
echo "Wrote $ECS_JSON"
if [[ "$NEEDS_VPC_NET" -eq 1 && ( "$SUBNETS_JSON" == "[]" || "$SECURITY_GROUPS_JSON" == "[]" ) ]]; then
  echo "No default VPC or subnets/SGs found; add subnets and security_groups to the file or set ECS_SUBNETS, ECS_SECURITY_GROUPS in env for Fargate/awsvpc invocations." >&2
fi
if [[ "$ECS_LAUNCH_TYPE" == "ec2" && ( "$ECS_NETWORK_MODE" == "bridge" || "$ECS_NETWORK_MODE" == "host" ) ]]; then
  echo "EC2 $ECS_NETWORK_MODE mode: subnets/security_groups not required for run_task (ensure container instances are registered in cluster $ECS_CLUSTER)." >&2
fi
