# Shared setup for extensions-service shell scripts.
# Disable AWS CLI pager so long JSON output does not block the terminal in less.
export AWS_PAGER="${AWS_PAGER:-}"

# Create CloudWatch log group if missing (idempotent).
# Usage: ensure_cloudwatch_log_group <log-group-name> [aws-region]
ensure_cloudwatch_log_group() {
  local log_group_name="$1"
  local region="${2:-${AWS_REGION:-us-east-1}}"
  if aws logs describe-log-groups --log-group-name-prefix "$log_group_name" --region "$region" \
    --query "logGroups[?logGroupName=='${log_group_name}'].logGroupName" --output text 2>/dev/null \
    | grep -qF "$log_group_name"; then
    return 0
  fi
  if aws logs create-log-group --log-group-name "$log_group_name" --region "$region" 2>/dev/null; then
    echo "Created CloudWatch log group: $log_group_name"
  fi
}
