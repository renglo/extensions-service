#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# View CloudWatch Logs for extension Handlers Lambda (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT. Args: [--follow] [--filter PATTERN] [--hours N]

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -n "${LAMBDA_FUNCTION_NAME:-}" ]]; then
  FUNCTION_NAME="$LAMBDA_FUNCTION_NAME"
elif [[ -n "${DEPLOY_INPUT_FILE:-}" && -f "${DEPLOY_INPUT_FILE}" ]]; then
  FUNCTION_NAME=$(python3 -c "
import sys
sys.path.insert(0, '${SERVICE_ROOT}')
from pathlib import Path
from deploy_input import build_lambda_config, deploy_input_from_path
payload = deploy_input_from_path(Path('${DEPLOY_INPUT_FILE}'))
print(build_lambda_config(payload, '${EXTENSION_NAME}')['FunctionName'])
")
else
  echo "ERROR: Set LAMBDA_FUNCTION_NAME or DEPLOY_INPUT_FILE (path to deploy_input.json)" >&2
  exit 1
fi
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

if ! aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$AWS_REGION" \
  --query "logGroups[?logGroupName=='${LOG_GROUP}'].logGroupName" --output text 2>/dev/null \
  | grep -qF "$LOG_GROUP"; then
  echo "Log group not found; creating $LOG_GROUP ..."
  ensure_cloudwatch_log_group "$LOG_GROUP" "$AWS_REGION"
fi

if ! aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$AWS_REGION" \
  --query "logGroups[?logGroupName=='${LOG_GROUP}'].logGroupName" --output text 2>/dev/null \
  | grep -qF "$LOG_GROUP"; then
  echo "ERROR: Log group $LOG_GROUP does not exist in $AWS_REGION." >&2
  echo "       Run: python3 run.py $EXTENSION_NAME deploy deploy --profile <aws-profile>" >&2
  echo "       Or invoke the function once (Lambda creates the group on first run if IAM allows it)." >&2
  exit 1
fi

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
