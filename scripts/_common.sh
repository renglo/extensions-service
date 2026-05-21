# Shared setup for extensions-service shell scripts.
# Disable AWS CLI pager so long JSON output does not block the terminal in less.
export AWS_PAGER="${AWS_PAGER:-}"

# Matches launcher/scripts (provision_backend_infra.py, bootstrap_github_oidc.py, etc.)
REGLO_DEPLOYMENT_DESCRIPTION="${REGLO_DEPLOYMENT_DESCRIPTION:-Reglo Deployment}"

# Apply IAM role description on create and refresh on existing roles.
reglo_ensure_iam_role_description() {
  local role_name="$1"
  local description="${2:-$REGLO_DEPLOYMENT_DESCRIPTION}"
  if ! aws iam get-role --role-name "$role_name" >/dev/null 2>&1; then
    return 0
  fi
  aws iam update-role-description --role-name "$role_name" --description "$description" >/dev/null 2>&1 || true
}

reglo_create_iam_role() {
  local role_name="$1"
  local trust_policy="$2"
  local description="${3:-$REGLO_DEPLOYMENT_DESCRIPTION}"
  aws iam create-role \
    --role-name "$role_name" \
    --assume-role-policy-document "$trust_policy" \
    --description "$description" >/dev/null
  reglo_ensure_iam_role_description "$role_name" "$description"
}

reglo_create_iam_policy() {
  local policy_name="$1"
  local policy_document="$2"
  local description="${3:-$REGLO_DEPLOYMENT_DESCRIPTION}"
  aws iam create-policy \
    --policy-name "$policy_name" \
    --policy-document "file://${policy_document}" \
    --description "$description" >/dev/null
}

reglo_tag_s3_bucket() {
  local bucket="$1"
  aws s3api put-bucket-tagging --bucket "$bucket" \
    --tagging "TagSet=[{Key=Description,Value=${REGLO_DEPLOYMENT_DESCRIPTION}}]" \
    >/dev/null 2>&1 || true
}

reglo_tag_ecr_repository() {
  local repository_name="$1"
  local region="${2:-${AWS_REGION:-us-east-1}}"
  local account
  account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
  [[ -z "$account" ]] && return 0
  aws ecr tag-resource \
    --resource-arn "arn:aws:ecr:${region}:${account}:repository/${repository_name}" \
    --tags "Key=Description,Value=${REGLO_DEPLOYMENT_DESCRIPTION}" \
    --region "$region" >/dev/null 2>&1 || true
}

reglo_tag_ecs_cluster() {
  local cluster_name="$1"
  local region="${2:-${AWS_REGION:-us-east-1}}"
  local account
  account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
  [[ -z "$account" ]] && return 0
  aws ecs tag-resource \
    --resource-arn "arn:aws:ecs:${region}:${account}:cluster/${cluster_name}" \
    --tags "key=Description,value=${REGLO_DEPLOYMENT_DESCRIPTION}" \
    --region "$region" >/dev/null 2>&1 || true
}

reglo_tag_ecs_capacity_provider() {
  local capacity_provider_name="$1"
  local region="${2:-${AWS_REGION:-us-east-1}}"
  local account
  account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
  [[ -z "$account" ]] && return 0
  aws ecs tag-resource \
    --resource-arn "arn:aws:ecs:${region}:${account}:capacity-provider/${capacity_provider_name}" \
    --tags "key=Description,value=${REGLO_DEPLOYMENT_DESCRIPTION}" \
    --region "$region" >/dev/null 2>&1 || true
}

reglo_tag_cloudwatch_log_group() {
  local log_group_name="$1"
  local region="${2:-${AWS_REGION:-us-east-1}}"
  aws logs tag-log-group \
    --log-group-name "$log_group_name" \
    --tags "Description=${REGLO_DEPLOYMENT_DESCRIPTION}" \
    --region "$region" >/dev/null 2>&1 || true
}

# Create CloudWatch log group if missing (idempotent).
# Usage: ensure_cloudwatch_log_group <log-group-name> [aws-region]
ensure_cloudwatch_log_group() {
  local log_group_name="$1"
  local region="${2:-${AWS_REGION:-us-east-1}}"
  if aws logs describe-log-groups --log-group-name-prefix "$log_group_name" --region "$region" \
    --query "logGroups[?logGroupName=='${log_group_name}'].logGroupName" --output text 2>/dev/null \
    | grep -qF "$log_group_name"; then
    reglo_tag_cloudwatch_log_group "$log_group_name" "$region"
    return 0
  fi
  if aws logs create-log-group --log-group-name "$log_group_name" --region "$region" 2>/dev/null; then
    echo "Created CloudWatch log group: $log_group_name"
    reglo_tag_cloudwatch_log_group "$log_group_name" "$region"
  fi
}
