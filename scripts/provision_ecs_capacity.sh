#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# Provision ECS EC2 capacity for one extension cluster:
# - instance role + instance profile
# - launch template (ECS-optimized AMI via SSM)
# - auto scaling group
# - capacity provider + cluster association
#
# Requires: EXTENSION_NAME, WORKSPACE_ROOT
# Optional: ECS_CLUSTER, AWS_REGION, AWS_PROFILE

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
ECS_CLUSTER="${ECS_CLUSTER:-${EXTENSION_NAME}-handlers}"

PROFILE_ENV="$(mktemp)"
python3 "$SERVICE_ROOT/ecs_profile.py" export-for-deploy "$WORKSPACE_ROOT" "$EXTENSION_NAME" > "$PROFILE_ENV"
# shellcheck disable=SC1090
source "$PROFILE_ENV"
rm -f "$PROFILE_ENV"

LAUNCH_TYPE="${ECS_PROFILE_LAUNCH_TYPE:-fargate}"
EC2_INSTANCE_TYPE="${ECS_PROFILE_EC2_INSTANCE_TYPE:-m5.xlarge}"
ASG_MIN="${ECS_PROFILE_ASG_MIN_SIZE:-1}"
ASG_DESIRED="${ECS_PROFILE_ASG_DESIRED_CAPACITY:-1}"
ASG_MAX="${ECS_PROFILE_ASG_MAX_SIZE:-1}"

if [[ "$LAUNCH_TYPE" != "ec2" ]]; then
  echo "launch_type=$LAUNCH_TYPE (not ec2). Skipping EC2 capacity provisioning."
  exit 0
fi

ECS_INSTANCE_ROLE_NAME="${EXTENSION_NAME}-handlers-ecs-instance"
ECS_INSTANCE_PROFILE_NAME="${EXTENSION_NAME}-handlers-ecs-instance-profile"
ECS_LT_NAME="${EXTENSION_NAME}-handlers-ecs-lt"
ECS_ASG_NAME="${EXTENSION_NAME}-handlers-ecs-asg"
ECS_CP_NAME="${EXTENSION_NAME}-handlers-ecs-cp"

AMI_SSM_PARAM="${ECS_EC2_AMI_SSM_PARAM:-/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id}"
AMI_ID="$(aws ssm get-parameter --name "$AMI_SSM_PARAM" --query 'Parameter.Value' --output text --region "$AWS_REGION")"
if [[ -z "$AMI_ID" || "$AMI_ID" == "None" ]]; then
  echo "ERROR: Could not resolve ECS AMI from SSM parameter: $AMI_SSM_PARAM" >&2
  exit 1
fi

echo "Resolved ECS AMI: $AMI_ID ($AMI_SSM_PARAM)"

DEFAULT_VPC_ID="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION" 2>/dev/null || true)"
if [[ -z "$DEFAULT_VPC_ID" || "$DEFAULT_VPC_ID" == "None" ]]; then
  echo "ERROR: No default VPC found. For now this script requires a default VPC (simplicity mode)." >&2
  exit 1
fi
SUBNETS="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$DEFAULT_VPC_ID" --query 'Subnets[*].SubnetId' --output text --region "$AWS_REGION" 2>/dev/null || true)"
if [[ -z "$SUBNETS" ]]; then
  echo "ERROR: No subnets found in default VPC $DEFAULT_VPC_ID" >&2
  exit 1
fi
# AWS CLI text output is tab-separated; normalize any whitespace to CSV.
SUBNETS_CSV="$(echo "$SUBNETS" | tr -s '[:space:]' ',' | sed 's/^,*//; s/,*$//')"
DEFAULT_SG_ID="$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$DEFAULT_VPC_ID" "Name=group-name,Values=default" --query 'SecurityGroups[0].GroupId' --output text --region "$AWS_REGION" 2>/dev/null || true)"
if [[ -z "$DEFAULT_SG_ID" || "$DEFAULT_SG_ID" == "None" ]]; then
  echo "ERROR: Could not resolve default security group in VPC $DEFAULT_VPC_ID" >&2
  exit 1
fi

echo "Using default VPC: $DEFAULT_VPC_ID"
echo "Using subnets: $SUBNETS"
echo "Using node security group: $DEFAULT_SG_ID"

echo "==> Ensure ECS instance role/profile..."
if ! aws iam get-role --role-name "$ECS_INSTANCE_ROLE_NAME" >/dev/null 2>&1; then
  TRUST_DOC="$(mktemp)"
  cat > "$TRUST_DOC" <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
JSON
  reglo_create_iam_role "$ECS_INSTANCE_ROLE_NAME" "file://$TRUST_DOC" >/dev/null
  rm -f "$TRUST_DOC"
  aws iam attach-role-policy --role-name "$ECS_INSTANCE_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role" >/dev/null
  aws iam attach-role-policy --role-name "$ECS_INSTANCE_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" >/dev/null
  echo "Created role $ECS_INSTANCE_ROLE_NAME"
else
  reglo_ensure_iam_role_description "$ECS_INSTANCE_ROLE_NAME"
fi

if ! aws iam get-instance-profile --instance-profile-name "$ECS_INSTANCE_PROFILE_NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$ECS_INSTANCE_PROFILE_NAME" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$ECS_INSTANCE_PROFILE_NAME" --role-name "$ECS_INSTANCE_ROLE_NAME" >/dev/null
  # eventual consistency
  sleep 5
  echo "Created instance profile $ECS_INSTANCE_PROFILE_NAME"
fi

echo "==> Create/update launch template..."
USER_DATA_B64="$(ECS_CLUSTER="$ECS_CLUSTER" python3 - <<'PY'
import base64
import os
cluster = os.environ["ECS_CLUSTER"]
script = f"""#!/bin/bash
echo ECS_CLUSTER={cluster} >> /etc/ecs/ecs.config
"""
print(base64.b64encode(script.encode()).decode())
PY
)"

LT_DATA="$(mktemp)"
cat > "$LT_DATA" <<JSON
{
  "ImageId": "$AMI_ID",
  "InstanceType": "$EC2_INSTANCE_TYPE",
  "IamInstanceProfile": { "Name": "$ECS_INSTANCE_PROFILE_NAME" },
  "SecurityGroupIds": ["$DEFAULT_SG_ID"],
  "UserData": "$USER_DATA_B64",
  "TagSpecifications": [
    {
      "ResourceType": "instance",
      "Tags": [
        {"Key":"Name","Value":"$ECS_ASG_NAME"},
        {"Key":"Project","Value":"$EXTENSION_NAME"},
        {"Key":"Service","Value":"handlers-ecs-ec2"},
        {"Key":"Description","Value":"$REGLO_DEPLOYMENT_DESCRIPTION"}
      ]
    }
  ]
}
JSON

if aws ec2 describe-launch-templates --launch-template-names "$ECS_LT_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws ec2 create-launch-template-version \
    --launch-template-name "$ECS_LT_NAME" \
    --source-version '$Latest' \
    --launch-template-data "file://$LT_DATA" \
    --region "$AWS_REGION" >/dev/null
  echo "Updated launch template $ECS_LT_NAME"
else
  aws ec2 create-launch-template \
    --launch-template-name "$ECS_LT_NAME" \
    --launch-template-data "file://$LT_DATA" \
    --region "$AWS_REGION" >/dev/null
  echo "Created launch template $ECS_LT_NAME"
fi
rm -f "$LT_DATA"

echo "==> Create/update ASG..."
if aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ECS_ASG_NAME" --query 'AutoScalingGroups[0].AutoScalingGroupName' --output text --region "$AWS_REGION" 2>/dev/null | grep -q "$ECS_ASG_NAME"; then
  aws autoscaling update-auto-scaling-group \
    --auto-scaling-group-name "$ECS_ASG_NAME" \
    --launch-template "LaunchTemplateName=$ECS_LT_NAME,Version=\$Latest" \
    --min-size "$ASG_MIN" \
    --desired-capacity "$ASG_DESIRED" \
    --max-size "$ASG_MAX" \
    --vpc-zone-identifier "$SUBNETS_CSV" \
    --region "$AWS_REGION" >/dev/null
  echo "Updated ASG $ECS_ASG_NAME (min=$ASG_MIN desired=$ASG_DESIRED max=$ASG_MAX)"
else
  aws autoscaling create-auto-scaling-group \
    --auto-scaling-group-name "$ECS_ASG_NAME" \
    --launch-template "LaunchTemplateName=$ECS_LT_NAME,Version=\$Latest" \
    --min-size "$ASG_MIN" \
    --desired-capacity "$ASG_DESIRED" \
    --max-size "$ASG_MAX" \
    --vpc-zone-identifier "$SUBNETS_CSV" \
    --tags "Key=Name,Value=$ECS_ASG_NAME,PropagateAtLaunch=true" "Key=Project,Value=$EXTENSION_NAME,PropagateAtLaunch=true" "Key=Service,Value=handlers-ecs-ec2,PropagateAtLaunch=true" "Key=Description,Value=${REGLO_DEPLOYMENT_DESCRIPTION},PropagateAtLaunch=true" \
    --region "$AWS_REGION" >/dev/null
  echo "Created ASG $ECS_ASG_NAME (min=$ASG_MIN desired=$ASG_DESIRED max=$ASG_MAX)"
fi

echo "==> Ensure capacity provider..."
ASG_ARN="$(aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names "$ECS_ASG_NAME" \
  --query 'AutoScalingGroups[0].AutoScalingGroupARN' \
  --output text --region "$AWS_REGION")"
if [[ -z "$ASG_ARN" || "$ASG_ARN" == "None" ]]; then
  echo "ERROR: Could not find ASG ARN for $ECS_ASG_NAME" >&2
  exit 1
fi

# describe-capacity-providers exits 0 even when CP does not exist (returns empty list).
# Query the status field explicitly to detect existence.
CP_STATUS="$(aws ecs describe-capacity-providers \
  --capacity-providers "$ECS_CP_NAME" \
  --region "$AWS_REGION" \
  --query 'capacityProviders[0].status' \
  --output text 2>/dev/null || true)"

if [[ "$CP_STATUS" == "ACTIVE" ]]; then
  echo "Capacity provider $ECS_CP_NAME already ACTIVE"
  reglo_tag_ecs_capacity_provider "$ECS_CP_NAME" "$AWS_REGION"
else
  aws ecs create-capacity-provider \
    --name "$ECS_CP_NAME" \
    --auto-scaling-group-provider "autoScalingGroupArn=${ASG_ARN},managedScaling={status=ENABLED,targetCapacity=100,minimumScalingStepSize=1,maximumScalingStepSize=4},managedTerminationProtection=DISABLED" \
    --region "$AWS_REGION" >/dev/null
  echo "Created capacity provider $ECS_CP_NAME"
  reglo_tag_ecs_capacity_provider "$ECS_CP_NAME" "$AWS_REGION"
  sleep 3
fi

echo "==> Associate capacity provider to cluster..."
aws ecs put-cluster-capacity-providers \
  --cluster "$ECS_CLUSTER" \
  --capacity-providers "$ECS_CP_NAME" \
  --default-capacity-provider-strategy "capacityProvider=$ECS_CP_NAME,weight=1,base=0" \
  --region "$AWS_REGION" >/dev/null

echo ""
echo "EC2 capacity provisioned:"
echo "  Cluster: $ECS_CLUSTER"
echo "  ASG: $ECS_ASG_NAME (min=$ASG_MIN desired=$ASG_DESIRED max=$ASG_MAX)"
echo "  Capacity provider: $ECS_CP_NAME"
echo ""
