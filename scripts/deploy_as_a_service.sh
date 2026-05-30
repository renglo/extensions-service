#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# Deploy extension Handlers as AWS Lambda (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT. Args: [deploy|update|undeploy] [--clean]

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_SCRIPT="$SCRIPT_DIR/build_lambda_package.sh"
if [[ -n "${DEPLOYMENT_ZIP:-}" ]]; then
  DEPLOYMENT_ZIP="$(cd "$(dirname "$DEPLOYMENT_ZIP")" && pwd)/$(basename "$DEPLOYMENT_ZIP")"
else
  DEPLOYMENT_ZIP=$(python3 -c "
import sys
sys.path.insert(0, '${SERVICE_ROOT}')
from state_store import get_state_paths
print(get_state_paths('${EXTENSION_NAME}').lambda_deployment_zip)
")
fi
GENERATED_LAMBDA_CONFIG=""

if [[ -n "${DEPLOY_INPUT_FILE:-}" && -f "${DEPLOY_INPUT_FILE}" ]]; then
  GENERATED_LAMBDA_CONFIG=$(mktemp)
  python3 "$SERVICE_ROOT/deploy_input.py" export-lambda-config "$DEPLOY_INPUT_FILE" \
    --extension "$EXTENSION_NAME" -o "$GENERATED_LAMBDA_CONFIG" || exit 1
  LAMBDA_CONFIG="$GENERATED_LAMBDA_CONFIG"
elif [[ -n "${LAMBDA_CONFIG_FILE:-}" && -f "${LAMBDA_CONFIG_FILE}" ]]; then
  LAMBDA_CONFIG="$LAMBDA_CONFIG_FILE"
else
  echo "ERROR: Set DEPLOY_INPUT_FILE (state/<ext>/deploy_input.json) or LAMBDA_CONFIG_FILE to a Lambda CLI JSON payload." >&2
  exit 1
fi

cleanup() {
  [[ -n "$GENERATED_LAMBDA_CONFIG" && -f "$GENERATED_LAMBDA_CONFIG" ]] && rm -f "$GENERATED_LAMBDA_CONFIG"
}
trap cleanup EXIT

FUNCTION_NAME=$(python3 -c "import json; print(json.load(open('$LAMBDA_CONFIG'))['FunctionName'])")
ACTION="${1:-deploy}"
CLEAN_BUILD=false

for arg in "$@"; do
  [[ "$arg" == "--clean" ]] && CLEAN_BUILD=true
done

if [[ "$ACTION" != "deploy" && "$ACTION" != "update" && "$ACTION" != "undeploy" ]]; then
  echo "ERROR: Action must be deploy, update, or undeploy" >&2
  exit 1
fi

# Wait until Lambda code/config update completes (avoids ResourceConflict on config update).
wait_for_lambda_updated() {
  local function_name="$1"
  local region="$2"
  local timeout_s="${3:-600}"
  local elapsed=0
  echo "==> Waiting for $function_name to become Active..."
  while (( elapsed < timeout_s )); do
    local state last_status reason
    if ! read -r state last_status <<< "$(aws lambda get-function-configuration \
      --function-name "$function_name" \
      --region "$region" \
      --query '[State, LastUpdateStatus]' \
      --output text \
      --no-cli-pager 2>/dev/null)"; then
      sleep 3
      elapsed=$((elapsed + 3))
      continue
    fi
    if [[ "$state" == "Active" && ( "$last_status" == "Successful" || "$last_status" == "None" || -z "$last_status" ) ]]; then
      echo "  Lambda ready (State=$state)"
      return 0
    fi
    if [[ "$last_status" == "Failed" ]]; then
      reason=$(aws lambda get-function-configuration \
        --function-name "$function_name" \
        --region "$region" \
        --query 'LastUpdateStatusReason' \
        --output text \
        --no-cli-pager 2>/dev/null || echo "unknown")
      echo "ERROR: Lambda update failed for $function_name: $reason" >&2
      return 1
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  echo "ERROR: Timed out after ${timeout_s}s waiting for $function_name" >&2
  return 1
}

echo "=========================================="
echo "Lambda Deployment: $EXTENSION_NAME ($FUNCTION_NAME)"
echo "Action: $ACTION"
[[ -n "${AWS_PROFILE:-}" ]] && echo "AWS Profile: $AWS_PROFILE"
echo "=========================================="
echo ""

if [[ "$ACTION" == "undeploy" ]]; then
  AWS_REGION="${AWS_REGION:-us-east-1}"
  if ! aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "Function $FUNCTION_NAME does not exist. Nothing to delete."
    exit 0
  fi
  aws lambda delete-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
  echo "Deleted $FUNCTION_NAME"
  exit 0
fi

if [[ ! -f "$LAMBDA_CONFIG" ]]; then
  echo "ERROR: $LAMBDA_CONFIG not found" >&2
  exit 1
fi

if [[ "$CLEAN_BUILD" == "true" ]]; then
  rm -f "$DEPLOYMENT_ZIP"
fi

if [[ ! -f "$DEPLOYMENT_ZIP" ]]; then
  echo "==> Building package..."
  OUTPUT_STATE_DIR="$(dirname "$DEPLOYMENT_ZIP")"
  EXTENSION_NAME="$EXTENSION_NAME" WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    OUTPUT_STATE_DIR="$OUTPUT_STATE_DIR" DEPLOYMENT_ZIP="$DEPLOYMENT_ZIP" \
    "$BUILD_SCRIPT" || exit 1
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
[[ -z "$AWS_ACCOUNT" ]] && { echo "ERROR: Could not get AWS account" >&2; exit 1; }

TEMP_CONFIG=$(mktemp)
ROLE_FROM_CONFIG=$(python3 -c "
import json
with open('$LAMBDA_CONFIG') as f:
    c = json.load(f)
r = c.get('Role', '${EXTENSION_NAME}-handlers-role')
print(r.split('/')[-1] if r.startswith('arn:aws:iam::') else r)
")
python3 -c "
import json
with open('$LAMBDA_CONFIG') as f:
    c = json.load(f)
r = c.get('Role', '${EXTENSION_NAME}-handlers-role')
name = r.split('/')[-1] if r.startswith('arn:aws:iam::') else r
c['Role'] = f'arn:aws:iam::${AWS_ACCOUNT}:role/{name}'
with open('$TEMP_CONFIG', 'w') as f:
    json.dump(c, f, indent=2)
"

if [[ "$ACTION" == "deploy" ]]; then
  if ! aws iam get-role --role-name "$ROLE_FROM_CONFIG" >/dev/null 2>&1; then
    echo "ERROR: IAM role '$ROLE_FROM_CONFIG' does not exist or Lambda cannot assume it." >&2
    echo "       Run this first: python3 run.py $EXTENSION_NAME setup-iam" >&2
    rm -f "$TEMP_CONFIG"
    exit 1
  fi
  if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "Deleting existing function for clean deploy..."
    aws lambda delete-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
    for i in $(seq 1 30); do
      aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || break
      sleep 1
    done
  fi
  aws lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --region "$AWS_REGION" \
    --zip-file "fileb://$DEPLOYMENT_ZIP" \
    --cli-input-json "file://$TEMP_CONFIG" \
    --no-cli-pager || { rm -f "$TEMP_CONFIG"; exit 1; }
else
  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --region "$AWS_REGION" \
    --zip-file "fileb://$DEPLOYMENT_ZIP" \
    --no-cli-pager || { rm -f "$TEMP_CONFIG"; exit 1; }
  wait_for_lambda_updated "$FUNCTION_NAME" "$AWS_REGION" || { rm -f "$TEMP_CONFIG"; exit 1; }
  python3 -c "
import json
with open('$TEMP_CONFIG') as f: c = json.load(f)
up = {k: c.get(k) for k in ('Role','Handler','Timeout','MemorySize','Environment','Description') if c.get(k)}
with open('$TEMP_CONFIG', 'w') as f: json.dump(up, f, indent=2)
"
  echo "==> Updating Lambda configuration (env vars)..."
  aws lambda update-function-configuration \
    --function-name "$FUNCTION_NAME" \
    --region "$AWS_REGION" \
    --cli-input-json "file://$TEMP_CONFIG" \
    --no-cli-pager || { rm -f "$TEMP_CONFIG"; exit 1; }
  wait_for_lambda_updated "$FUNCTION_NAME" "$AWS_REGION" || { rm -f "$TEMP_CONFIG"; exit 1; }
fi

rm -f "$TEMP_CONFIG"

LAMBDA_LOG_GROUP="/aws/lambda/${FUNCTION_NAME}"
echo "==> CloudWatch log group..."
ensure_cloudwatch_log_group "$LAMBDA_LOG_GROUP" "$AWS_REGION"

echo ""
echo "Deployment complete: $FUNCTION_NAME ($AWS_REGION)"
echo "Logs: aws logs tail $LAMBDA_LOG_GROUP --follow --region $AWS_REGION"
echo "      python3 run.py $EXTENSION_NAME view-logs --follow"
echo ""
