#!/usr/bin/env bash
set -euo pipefail

# View CloudWatch Logs for extension Handlers Lambda (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT. Args: [--follow] [--filter PATTERN] [--hours N]

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

LAMBDA_CONFIG="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/installer/service/lambda_config.json"
FUNCTION_NAME=$(python3 -c "import json; print(json.load(open('$LAMBDA_CONFIG'))['FunctionName'])")
LOG_GROUP="/aws/lambda/${FUNCTION_NAME}"
AWS_REGION="${AWS_REGION:-us-east-1}"
FOLLOW=false
FILTER_PATTERN=""
HOURS=1

while [[ $# -gt 0 ]]; do
  case $1 in
    --follow|-f) FOLLOW=true; shift ;;
    --filter) FILTER_PATTERN="$2"; shift 2 ;;
    --hours) HOURS="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--follow] [--filter PATTERN] [--hours N]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

command -v aws >/dev/null 2>&1 || { echo "ERROR: AWS CLI not installed" >&2; exit 1; }

echo "=========================================="
echo "CloudWatch Logs: $LOG_GROUP"
echo "=========================================="
echo ""

if [[ "$FOLLOW" == "true" ]]; then
  if [[ -n "$FILTER_PATTERN" ]]; then
    aws logs tail "$LOG_GROUP" --region "$AWS_REGION" --follow --format short --filter-pattern "$FILTER_PATTERN"
  else
    aws logs tail "$LOG_GROUP" --region "$AWS_REGION" --follow --format short
  fi
else
  START_TIME=$(($(date +%s) - HOURS * 3600))000
  aws logs filter-log-events --log-group-name "$LOG_GROUP" --region "$AWS_REGION" --start-time "$START_TIME" \
    ${FILTER_PATTERN:+--filter-pattern "$FILTER_PATTERN"} \
    --query 'events[*].[timestamp,message]' --output text | while IFS=$'\t' read -r ts msg; do
    unix_time=$((ts / 1000))
    date_str=$(date -r "${unix_time}" '+%Y-%m-%d %H:%M:%S' 2>/dev/null) || date_str=$(date -d "@${unix_time}" '+%Y-%m-%d %H:%M:%S' 2>/dev/null) || date_str="N/A"
    echo "[$date_str] $msg"
  done
fi
