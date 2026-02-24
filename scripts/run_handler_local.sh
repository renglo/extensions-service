#!/usr/bin/env bash
set -euo pipefail

# Run extension handlers locally via Docker (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT. Args: <handler_name> [payload_file.json] [--rebuild]

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/package"
# Prefer :local (arm64) if it exists (from "build --local"); else use :latest (amd64). No flag needed on run-local.
if docker image inspect "${EXTENSION_NAME}-lambda-builder:local" >/dev/null 2>&1; then
  DOCKER_IMAGE="${EXTENSION_NAME}-lambda-builder:local"
  RUN_PLATFORM="linux/arm64"
else
  DOCKER_IMAGE="${EXTENSION_NAME}-lambda-builder:latest"
  RUN_PLATFORM="linux/amd64"
fi

[[ $# -lt 1 ]] && { echo "Usage: $0 <handler_name> [payload_file.json] [--rebuild]" >&2; exit 1; }

REBUILD=false
ARGS=()
for a in "$@"; do
  [[ "$a" == "--rebuild" ]] && REBUILD=true || ARGS+=("$a")
done

HANDLER_NAME="${ARGS[0]}"
PAYLOAD_FILE="${ARGS[1]:-}"
PAYLOAD_JSON="${ARGS[2]:-}"

if [[ -n "$PAYLOAD_JSON" && "$PAYLOAD_FILE" == "--payload" ]]; then
  PAYLOAD_CONTENT="$PAYLOAD_JSON"
elif [[ -n "$PAYLOAD_FILE" && -f "$PAYLOAD_FILE" ]]; then
  PAYLOAD_CONTENT="$(cat "$PAYLOAD_FILE")"
elif [[ -n "$PAYLOAD_FILE" ]]; then
  echo "ERROR: Payload file not found: $PAYLOAD_FILE" >&2
  exit 1
else
  PAYLOAD_CONTENT="{}"
fi

echo "$PAYLOAD_CONTENT" | python3 -m json.tool >/dev/null 2>&1 || { echo "ERROR: Invalid JSON payload" >&2; exit 1; }

EVENT_FILE=$(mktemp)
python3 -c "
import json, sys
payload = json.loads(sys.stdin.read())
json.dump({'handler': '$HANDLER_NAME', 'payload': payload}, open('$EVENT_FILE', 'w'))
" <<< "$PAYLOAD_CONTENT"

echo "=========================================="
echo "Running Handler: $EXTENSION_NAME / $HANDLER_NAME"
echo "=========================================="

BUILD_FOR_LOCAL="0"
[[ "$DOCKER_IMAGE" == *:local ]] && BUILD_FOR_LOCAL="1"

if [[ "$REBUILD" == "true" ]]; then
  docker rmi "$DOCKER_IMAGE" 2>/dev/null || true
  EXTENSION_NAME="$EXTENSION_NAME" WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    EXTENSION_SERVICE_NATIVE_PLATFORM="$BUILD_FOR_LOCAL" \
    "$SCRIPT_DIR/build_lambda_package.sh" || exit 1
elif ! docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
  EXTENSION_NAME="$EXTENSION_NAME" WORKSPACE_ROOT="$WORKSPACE_ROOT" \
    EXTENSION_SERVICE_NATIVE_PLATFORM="$BUILD_FOR_LOCAL" \
    "$SCRIPT_DIR/build_lambda_package.sh" || exit 1
fi

AWS_MOUNT=()
[[ -d "$HOME/.aws" ]] && AWS_MOUNT=(-v "$HOME/.aws:/root/.aws:ro")
AWS_ENV=()
[[ -n "${AWS_PROFILE:-}" ]] && AWS_ENV+=(-e "AWS_PROFILE=$AWS_PROFILE")
[[ -n "${AWS_ACCESS_KEY_ID:-}" ]] && AWS_ENV+=(-e "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID")
[[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]] && AWS_ENV+=(-e "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY")
[[ -n "${AWS_DEFAULT_REGION:-}" ]] && AWS_ENV+=(-e "AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION")
[[ -n "${AWS_REGION:-}" ]] && AWS_ENV+=(-e "AWS_REGION=$AWS_REGION")

ENV_CONFIG_FILE="$WORKSPACE_ROOT/system/env_config.py"
if [[ -f "$ENV_CONFIG_FILE" ]]; then
  TEMP_ENV=$(mktemp)
  python3 -c "
import ast, shlex
with open('$ENV_CONFIG_FILE') as f: tree = ast.parse(f.read())
for n in ast.walk(tree):
  if isinstance(n, ast.Assign):
    for t in n.targets:
      if isinstance(t, ast.Name) and t.id.isupper():
        v = getattr(n.value, 'value', getattr(n.value, 's', None))
        if v is not None and v != '': print(f\"{t.id}={shlex.quote(str(v))}\")
" 2>/dev/null > "$TEMP_ENV" || true
  while IFS='=' read -r k v; do [[ -n "$k" && -n "$v" ]] && AWS_ENV+=(-e "$k=$v"); done < "$TEMP_ENV" 2>/dev/null || true
  rm -f "$TEMP_ENV"
fi

TEMP_SCRIPT=$(mktemp)
trap "rm -f '$EVENT_FILE' '$TEMP_SCRIPT'" EXIT
cat > "$TEMP_SCRIPT" << 'PYEOF'
import sys, json
sys.path.insert(0, '/var/lang/lib/python3.12/site-packages')
sys.path.insert(0, '/build/output')
sys.path.insert(0, '/package')
from lambda_router import lambda_handler
with open('/tmp/event.json') as f:
    event = json.load(f)
try:
    print(json.dumps(lambda_handler(event, None), indent=2))
except Exception as e:
    import traceback
    print(json.dumps({'statusCode':500,'success':False,'error':str(e),'traceback':traceback.format_exc()}, indent=2))
    sys.exit(1)
PYEOF

docker run --rm --platform "$RUN_PLATFORM" --entrypoint /bin/sh \
  -v "$PACKAGE_DIR:/package" \
  -v "$EVENT_FILE:/tmp/event.json:ro" \
  -v "$TEMP_SCRIPT:/tmp/run_handler.py:ro" \
  "${AWS_MOUNT[@]}" "${AWS_ENV[@]}" \
  -w /package "$DOCKER_IMAGE" \
  -c "python3.12 /tmp/run_handler.py" || exit 1
