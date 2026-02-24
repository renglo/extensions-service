#!/usr/bin/env bash
set -euo pipefail

# Deploy extension Handlers as AWS Lambda (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT. Args: [deploy|update|undeploy] [--clean]

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/package"
SERVICE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/installer/service"
LAMBDA_CONFIG="$SERVICE_DIR/lambda_config.json"
BUILD_SCRIPT="$SCRIPT_DIR/build_lambda_package.sh"
DEPLOYMENT_ZIP="$PACKAGE_DIR/lambda_deployment.zip"

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
  EXTENSION_NAME="$EXTENSION_NAME" WORKSPACE_ROOT="$WORKSPACE_ROOT" "$BUILD_SCRIPT" || exit 1
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
    echo "       Run this first: python3 dev/extension-service/run.py $EXTENSION_NAME setup-iam" >&2
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
    --cli-input-json "file://$TEMP_CONFIG" || { rm -f "$TEMP_CONFIG"; exit 1; }
else
  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --region "$AWS_REGION" \
    --zip-file "fileb://$DEPLOYMENT_ZIP" || { rm -f "$TEMP_CONFIG"; exit 1; }
  python3 -c "
import json
with open('$TEMP_CONFIG') as f: c = json.load(f)
up = {k: c.get(k) for k in ('Role','Handler','Timeout','MemorySize','Environment','Description') if c.get(k)}
with open('$TEMP_CONFIG', 'w') as f: json.dump(up, f, indent=2)
"
  aws lambda update-function-configuration \
    --function-name "$FUNCTION_NAME" \
    --region "$AWS_REGION" \
    --cli-input-json "file://$TEMP_CONFIG" 2>/dev/null || true
fi

rm -f "$TEMP_CONFIG"
echo ""
echo "Deployment complete: $FUNCTION_NAME ($AWS_REGION)"
echo "Logs: aws logs tail /aws/lambda/$FUNCTION_NAME --follow --region $AWS_REGION"
echo ""
