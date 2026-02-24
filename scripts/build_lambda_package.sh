#!/usr/bin/env bash
set -euo pipefail

# Build Lambda deployment package using Docker (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT in environment (set by run.py).

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set (run via: python dev/extension-service/run.py <ext> build)" >&2
  exit 1
fi

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/package"
SERVICE_DIR="$WORKSPACE_ROOT/extensions/$EXTENSION_NAME/installer/service"
RENGLO_LIB_DIR="$WORKSPACE_ROOT/dev/renglo-lib"
BUILD_DIR="$PACKAGE_DIR/.lambda_build"
OUTPUT_ZIP="$PACKAGE_DIR/lambda_deployment.zip"
# Build for Lambda (amd64) by default; set EXTENSION_SERVICE_NATIVE_PLATFORM=1 for local-only arm64 image.
if [[ "${EXTENSION_SERVICE_NATIVE_PLATFORM:-0}" == "1" ]]; then
  DOCKER_PLATFORM="linux/arm64"
  DOCKER_IMAGE="${EXTENSION_NAME}-lambda-builder:local"
  EXTRACT_ZIP=false
  echo "=========================================="
  echo "Building local-only image (arm64): $EXTENSION_NAME"
  echo "=========================================="
else
  DOCKER_PLATFORM="linux/amd64"
  DOCKER_IMAGE="${EXTENSION_NAME}-lambda-builder:latest"
  EXTRACT_ZIP=true
  echo "=========================================="
  echo "Building Lambda Deployment Package (amd64): $EXTENSION_NAME"
  echo "=========================================="
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

# Dockerfile with EXTENSION_NAME expanded (unquoted heredoc)
cat > "$BUILD_DIR/Dockerfile" << DOCKERFILE
FROM public.ecr.aws/lambda/python:3.12

RUN microdnf install -y zip && microdnf clean all

RUN python3.12 -m pip install --upgrade pip setuptools wheel && \\
    pip install "PyYAML>=6.0" && \\
    python3.12 -c "import yaml; print('PyYAML installed in image, version:', yaml.__version__)"

WORKDIR /build

COPY extensions/${EXTENSION_NAME}/package/ /build/package/
COPY dev/renglo-lib/ /build/renglo-lib/

RUN set -e && \\
    cd /build && \\
    python3.12 -m pip install --upgrade pip setuptools wheel -q && \\
    mkdir -p /build/output && \\
    python3.12 -m pip install --no-cache-dir --target /build/output /build/renglo-lib && \\
    python3.12 -m pip install --no-cache-dir build wheel setuptools-scm 2>&1 | tail -5 || true && \\
    python3.12 -c "import tomllib, subprocess, sys; deps=tomllib.load(open('/build/package/pyproject.toml','rb'))['project']['dependencies']; [subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', '--target', '/build/output', d], check=False) or True for d in deps]" 2>&1 | tail -60 && \\
    echo "Checking if ${EXTENSION_NAME} package was installed..." && \\
    (test -d /build/output/${EXTENSION_NAME} && echo "  ✓ ${EXTENSION_NAME} directory found" || echo "  ✗ ${EXTENSION_NAME} NOT found - will copy source") && \\
    cp -r /build/package/${EXTENSION_NAME} /build/output/ && \\
    cp /build/package/lambda_router.py /build/output/ && \\
    cp /build/package/handlers_config.json /build/output/ 2>/dev/null || true && \\
    python3.12 -c "import sys; sys.path.insert(0, '/build/output'); import yaml; print('✓ yaml OK')" && \\
    cd /build/output && \\
    find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type f -name '*.pyc' -delete 2>/dev/null || true && \\
    find . -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type f -name '*.md' -delete 2>/dev/null || true && \\
    find . -type d -name 'examples' -exec rm -rf {} + 2>/dev/null || true && \\
    zip -r /build/lambda_deployment.zip . -q && \\
    echo "Build complete!"
DOCKERFILE

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
  echo ""
else
  echo "==> Skipping zip extraction (local-only image; use default build for Lambda deploy)"
  echo ""
  echo "=========================================="
  echo "Build complete!"
  echo "=========================================="
  echo "Image: $DOCKER_IMAGE (use with run-local and EXTENSION_SERVICE_NATIVE_PLATFORM=1)"
  echo ""
fi

rm -rf "$BUILD_DIR"
