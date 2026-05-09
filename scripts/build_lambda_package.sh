#!/usr/bin/env bash
set -euo pipefail

# Build Lambda deployment package using Docker (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT in environment (set by run.py).

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set (run via: python dev/extension-service/run.py <ext> build)" >&2
  exit 1
fi

# Extra extensions to bundle alongside the primary one (comma-separated, e.g. "pes,schd").
# Set by run.py from EXTERNAL_HANDLERS in env_config.py.
EXTRA_EXTENSIONS="${EXTRA_EXTENSIONS:-}"
EXTRA_EXT_ARRAY=()
if [[ -n "$EXTRA_EXTENSIONS" ]]; then
  IFS=',' read -ra _RAW_EXTS <<< "$EXTRA_EXTENSIONS"
  for _ext in "${_RAW_EXTS[@]}"; do
    _ext="${_ext// /}"
    [[ -z "$_ext" ]] && continue
    if [[ ! -d "$WORKSPACE_ROOT/extensions/$_ext/package" ]]; then
      echo "WARNING: Extra extension '$_ext' has no package directory — skipping." >&2
    else
      EXTRA_EXT_ARRAY+=("$_ext")
    fi
  done
fi

# Build Dockerfile COPY lines and RUN steps for each extra extension.
EXTRA_COPY_LINES=""
EXTRA_BUILD_STEPS=""
for _ext in "${EXTRA_EXT_ARRAY[@]}"; do
  EXTRA_COPY_LINES="${EXTRA_COPY_LINES}COPY extensions/${_ext}/package/ /build/package-${_ext}/"$'\n'
  EXTRA_BUILD_STEPS="${EXTRA_BUILD_STEPS}    echo \"==> Installing extra extension: ${_ext}\" && \\"$'\n'
  EXTRA_BUILD_STEPS="${EXTRA_BUILD_STEPS}    python3.12 -c \"import tomllib, subprocess, sys; f=open('/build/package-${_ext}/pyproject.toml','rb'); deps=tomllib.load(f)['project'].get('dependencies',[]); f.close(); [subprocess.run([sys.executable,'-m','pip','install','--no-cache-dir','--target','/build/output',d],check=False) for d in deps]\" 2>&1 | tail -20 && \\"$'\n'
  EXTRA_BUILD_STEPS="${EXTRA_BUILD_STEPS}    cp -r /build/package-${_ext}/${_ext} /build/output/ && \\"$'\n'
  # Merge handlers_config.json if the extra extension has one
  EXTRA_BUILD_STEPS="${EXTRA_BUILD_STEPS}    ( [ -f /build/package-${_ext}/handlers_config.json ] && python3.12 -c \"import json, pathlib; base=pathlib.Path('/build/output/handlers_config.json'); extra=pathlib.Path('/build/package-${_ext}/handlers_config.json'); merged={**json.loads(base.read_text())['handlers'],**json.loads(extra.read_text())['handlers']}; base.write_text(json.dumps({'handlers':merged},indent=2)); print('Merged handlers_config.json with ${_ext}')\" || echo 'No handlers_config.json for ${_ext} — bundled as library only' ) && \\"$'\n'
done

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/package"
SERVICE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/installer/service"
RENGLO_LIB_DIR="$WORKSPACE_ROOT/dev/renglo-lib"
BUILD_DIR="$PACKAGE_DIR/.lambda_build"
OUTPUT_ZIP="$PACKAGE_DIR/lambda_deployment.zip"
# Build for Lambda (amd64) by default; set EXTENSION_SERVICE_NATIVE_PLATFORM=1 for local-only arm64 image.
# Set EXTENSION_SERVICE_LARGE_BUILD=1 for ECS (installs [large-dependencies] extra, outputs ecs-builder image, no zip).
BUILD_LARGE="${EXTENSION_SERVICE_LARGE_BUILD:-0}"
if [[ "${EXTENSION_SERVICE_NATIVE_PLATFORM:-0}" == "1" ]]; then
  DOCKER_PLATFORM="linux/arm64"
  if [[ "$BUILD_LARGE" == "1" ]]; then
    DOCKER_IMAGE="${EXTENSION_NAME}-ecs-builder:local"
  else
    DOCKER_IMAGE="${EXTENSION_NAME}-lambda-builder:local"
  fi
  EXTRACT_ZIP=false
  echo "=========================================="
  echo "Building local-only image (arm64): $EXTENSION_NAME $([[ "$BUILD_LARGE" == "1" ]] && echo '(ECS large)' || true)"
  echo "=========================================="
else
  DOCKER_PLATFORM="linux/amd64"
  if [[ "$BUILD_LARGE" == "1" ]]; then
    DOCKER_IMAGE="${EXTENSION_NAME}-ecs-builder:latest"
    EXTRACT_ZIP=false
    echo "=========================================="
    echo "Building ECS large image (amd64): $EXTENSION_NAME"
    echo "=========================================="
  else
    DOCKER_IMAGE="${EXTENSION_NAME}-lambda-builder:latest"
    EXTRACT_ZIP=true
    echo "=========================================="
    echo "Building Lambda Deployment Package (amd64): $EXTENSION_NAME"
    echo "=========================================="
  fi
fi
echo ""

if [[ ! -d "$PACKAGE_DIR" ]]; then
  echo "ERROR: Package directory not found: $PACKAGE_DIR" >&2
  exit 1
fi

if [[ -d "$BUILD_DIR" ]]; then
  echo "==> Cleaning previous build..."
  rm -rf "$BUILD_DIR"
fi

mkdir -p "$BUILD_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed or not in PATH" >&2
  exit 1
fi

echo "==> Using Docker to build Lambda-compatible package..."
echo ""

# When BUILD_LARGE=1: inject pip install [large-dependencies] with retry (wheel-only for wheel_libs.json); no if inside container.
# When BUILD_LARGE=0: inject zip step. This way the RUN content differs and always runs correctly.
RUN_LARGE_DEPS_LINE=""
if [[ "$BUILD_LARGE" == "1" ]]; then
  WHEEL_LIBS_FILE="${SCRIPT_DIR}/../wheel_libs.json"
  PIP_ONLY_BINARY_LIST=""
  if [[ -f "$WHEEL_LIBS_FILE" ]]; then
    PIP_ONLY_BINARY_LIST=$(python3 -c "import json; print(','.join(json.load(open('$WHEEL_LIBS_FILE'))))" 2>/dev/null || true)
  fi
  # First try: normal pip install. If it fails, retry with PIP_ONLY_BINARY for packages in wheel_libs.json. If both fail, exit 1.
  RUN_LARGE_DEPS_LINE="    ( python3.12 -m pip install --no-cache-dir --target /build/output \"/build/package[large-dependencies]\" && ( python3.12 -c \"import sys; sys.path.insert(0,\\\"/build/output\\\"); import numpy; print(\\\"✓ large-dependencies OK\\\")\" ) ) || ( PIP_ONLY_BINARY=${PIP_ONLY_BINARY_LIST} python3.12 -m pip install --no-cache-dir --target /build/output \"/build/package[large-dependencies]\" && ( python3.12 -c \"import sys; sys.path.insert(0,\\\"/build/output\\\"); import numpy; print(\\\"✓ large-dependencies OK (wheel retry)\\\")\" ) ) || { echo \"ERROR: large-dependencies install failed or numpy missing\" >&2; exit 1; } && \\"
fi
if [[ "$BUILD_LARGE" == "1" ]]; then
  RUN_ZIP_LINE='    true && \'
else
  RUN_ZIP_LINE='    zip -r /build/lambda_deployment.zip . -q && \'
fi
echo "DEBUG: BUILD_LARGE=$BUILD_LARGE RUN_LARGE_DEPS_LINE length=${#RUN_LARGE_DEPS_LINE}"
if [[ ${#EXTRA_EXT_ARRAY[@]} -gt 0 ]]; then
  echo "DEBUG: Extra extensions to bundle: ${EXTRA_EXT_ARRAY[*]}"
fi

# Dockerfile with EXTENSION_NAME and RUN_LARGE_DEPS_LINE/RUN_ZIP_LINE expanded (unquoted heredoc)
cat > "$BUILD_DIR/Dockerfile" << DOCKERFILE
FROM public.ecr.aws/lambda/python:3.12

RUN microdnf install -y zip && microdnf clean all

RUN python3.12 -m pip install --upgrade pip setuptools wheel && \\
    pip install "PyYAML>=6.0" && \\
    python3.12 -c "import yaml; print('PyYAML installed in image, version:', yaml.__version__)"

WORKDIR /build

COPY extensions/${EXTENSION_NAME}/package/ /build/package/
COPY dev/renglo-lib/ /build/renglo-lib/
${EXTRA_COPY_LINES}
RUN set -e && \\
    cd /build && \\
    python3.12 -m pip install --upgrade pip setuptools wheel -q && \\
    mkdir -p /build/output && \\
    python3.12 -m pip install --no-cache-dir --target /build/output /build/renglo-lib && \\
    python3.12 -m pip install --no-cache-dir build wheel setuptools-scm 2>&1 && \\
    python3.12 -c "import tomllib, subprocess, sys; deps=tomllib.load(open('/build/package/pyproject.toml','rb'))['project']['dependencies']; [subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', '--target', '/build/output', d], check=False) or True for d in deps]" 2>&1 | tail -60 && \\
    $RUN_LARGE_DEPS_LINE
    echo "Checking if ${EXTENSION_NAME} package was installed..." && \\
    (test -d /build/output/${EXTENSION_NAME} && echo "  ✓ ${EXTENSION_NAME} directory found" || echo "  ✗ ${EXTENSION_NAME} NOT found - will copy source") && \\
    cp -r /build/package/${EXTENSION_NAME} /build/output/ && \\
    cp /build/package/lambda_router.py /build/output/ && \\
    cp /build/package/handlers_config.json /build/output/ 2>/dev/null || true && \\
    python3.12 -c "import sys; sys.path.insert(0, '/build/output'); import yaml; print('✓ yaml OK')" && \\
${EXTRA_BUILD_STEPS}    cd /build/output && \\
    find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type f -name '*.pyc' -delete 2>/dev/null || true && \\
    find . -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type f -name '*.md' -delete 2>/dev/null || true && \\
    find . -type d -name 'examples' -exec rm -rf {} + 2>/dev/null || true && \\
    $RUN_ZIP_LINE
    echo "Build complete!"
DOCKERFILE

if [[ "$BUILD_LARGE" == "1" ]]; then
  cat >> "$BUILD_DIR/Dockerfile" << 'DOCKERFILE_ECS'
COPY dev/extensions-service/scripts/ecs_handler_entrypoint.py /ecs_entrypoint.py
WORKDIR /build/output
ENTRYPOINT ["python3.12", "/ecs_entrypoint.py"]
DOCKERFILE_ECS
fi

echo "==> Building Docker image ($DOCKER_PLATFORM)..."
cd "$WORKSPACE_ROOT"
docker build \
  --platform "$DOCKER_PLATFORM" \
  --no-cache \
  -f "$BUILD_DIR/Dockerfile" \
  -t "$DOCKER_IMAGE" \
  . || {
  echo "ERROR: Docker build failed" >&2
  exit 1
}

if [[ "$EXTRACT_ZIP" == "true" ]]; then
  echo "==> Extracting deployment package (for Lambda upload)..."
  docker run --rm \
    --platform "$DOCKER_PLATFORM" \
    --entrypoint /bin/sh \
    -v "$PACKAGE_DIR:/output" \
    "$DOCKER_IMAGE" \
    -c "cp /build/lambda_deployment.zip /output/ && chmod 644 /output/lambda_deployment.zip" || {
    echo "ERROR: Failed to extract deployment package" >&2
    exit 1
  }

  if [[ ! -f "$OUTPUT_ZIP" ]]; then
    echo "ERROR: Failed to create deployment package" >&2
    exit 1
  fi
  ZIP_SIZE=$(du -h "$OUTPUT_ZIP" | cut -f1)
  echo ""
  echo "=========================================="
  echo "Build complete!"
  echo "=========================================="
  echo "Package: $OUTPUT_ZIP"
  echo "Size: $ZIP_SIZE"
  echo "Primary extension: $EXTENSION_NAME"
  if [[ ${#EXTRA_EXT_ARRAY[@]} -gt 0 ]]; then
    echo "Bundled extras:    ${EXTRA_EXT_ARRAY[*]}"
  fi
  echo ""
else
  echo "==> Skipping zip extraction (local-only or ECS image)"
  echo ""
  echo "=========================================="
  echo "Build complete!"
  echo "=========================================="
  echo "Image: $DOCKER_IMAGE"
  echo "Primary extension: $EXTENSION_NAME"
  if [[ ${#EXTRA_EXT_ARRAY[@]} -gt 0 ]]; then
    echo "Bundled extras:    ${EXTRA_EXT_ARRAY[*]}"
  fi
  if [[ "$BUILD_LARGE" == "1" ]]; then
    echo "(ECS large image; deploy with --type ecs)"
  else
    echo "(use with run-local and EXTENSION_SERVICE_NATIVE_PLATFORM=1)"
  fi
  echo ""
fi

rm -rf "$BUILD_DIR"
