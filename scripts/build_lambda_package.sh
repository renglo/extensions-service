#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# Build Lambda deployment package using Docker (shared script).
# Requires: EXTENSION_NAME, WORKSPACE_ROOT, HANDLERS_WHEELHOUSE, HANDLERS_ASSETS,
# HANDLERS_PACKAGES (from prepare_handlers_wheelhouse.py).
# Optional: OUTPUT_STATE_DIR, DEPLOYMENT_ZIP, RENGLO_LIB_COPY_PATH (default: dev/renglo-lib).
#
# ECS/--large is not supported here yet (same wheelhouse contract comes next).

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set (run via: python run.py <env> build)" >&2
  exit 1
fi

RENGLO_LIB_COPY_PATH="${RENGLO_LIB_COPY_PATH:-dev/renglo-lib}"
HANDLERS_WHEELHOUSE="${HANDLERS_WHEELHOUSE:-}"
HANDLERS_ASSETS="${HANDLERS_ASSETS:-}"
HANDLERS_PACKAGES="${HANDLERS_PACKAGES:-}"

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUILD_LARGE="${EXTENSION_SERVICE_LARGE_BUILD:-0}"
if [[ "$BUILD_LARGE" == "1" ]]; then
  echo "ERROR: ECS/--large image build is not supported yet." >&2
  echo "  Use Lambda/small wheelhouse builds (--no-ecs --wheelhouse/--assets/--packages)." >&2
  echo "  Large will use the same prepare→wheelhouse contract in a follow-up." >&2
  exit 1
fi

if [[ -z "$HANDLERS_WHEELHOUSE" || -z "$HANDLERS_ASSETS" || -z "$HANDLERS_PACKAGES" ]]; then
  echo "ERROR: handlers build requires a package wheelhouse." >&2
  echo "  Prepare: python ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \\" >&2
  echo "             --from-monorepo extensions/<ext>/package,... --out .handlers-build" >&2
  echo "  Build:   python ops/extensions-service/run.py <env> build --no-ecs \\" >&2
  echo "             --wheelhouse .handlers-build/wheelhouse \\" >&2
  echo "             --assets .handlers-build/handlers-assets \\" >&2
  echo "             --packages <dist[,dist...]> [--local]" >&2
  exit 1
fi

if [[ -z "${OUTPUT_STATE_DIR:-}" ]]; then
  OUTPUT_STATE_DIR=$(python3 -c "
import sys
sys.path.insert(0, '${SERVICE_ROOT}')
from state_store import get_state_paths
print(get_state_paths('${EXTENSION_NAME}').state_dir)
")
fi
OUTPUT_STATE_DIR="$(mkdir -p "$OUTPUT_STATE_DIR" && cd "$OUTPUT_STATE_DIR" && pwd)"
DEPLOYMENT_ZIP="${DEPLOYMENT_ZIP:-$OUTPUT_STATE_DIR/lambda_deployment.zip}"
BUILD_DIR="$OUTPUT_STATE_DIR/.lambda_build"
OUTPUT_ZIP="$DEPLOYMENT_ZIP"

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

if [[ ! -d "$HANDLERS_WHEELHOUSE" ]]; then
  echo "ERROR: HANDLERS_WHEELHOUSE not found: $HANDLERS_WHEELHOUSE" >&2
  exit 1
fi
if [[ ! -d "$HANDLERS_ASSETS" ]]; then
  echo "ERROR: HANDLERS_ASSETS not found: $HANDLERS_ASSETS" >&2
  exit 1
fi
if [[ ! -f "$HANDLERS_ASSETS/lambda_router.py" ]]; then
  echo "ERROR: missing $HANDLERS_ASSETS/lambda_router.py" >&2
  exit 1
fi
if [[ ! -f "$HANDLERS_ASSETS/handlers_config.json" ]]; then
  echo "ERROR: missing $HANDLERS_ASSETS/handlers_config.json" >&2
  exit 1
fi

if [[ ! -d "$WORKSPACE_ROOT/$RENGLO_LIB_COPY_PATH" ]]; then
  echo "ERROR: renglo-lib not found at $WORKSPACE_ROOT/$RENGLO_LIB_COPY_PATH" >&2
  echo "Set RENGLO_LIB_COPY_PATH or place the clone under the Docker context." >&2
  exit 1
fi

mkdir -p "$OUTPUT_STATE_DIR"

if [[ -d "$BUILD_DIR" ]]; then
  echo "==> Cleaning previous build..."
  rm -rf "$BUILD_DIR"
fi

mkdir -p "$BUILD_DIR"

echo "==> Staging wheelhouse + handlers-assets into build context..."
rm -rf "$BUILD_DIR/wheelhouse" "$BUILD_DIR/handlers-assets"
mkdir -p "$BUILD_DIR/wheelhouse" "$BUILD_DIR/handlers-assets"
cp -a "$HANDLERS_WHEELHOUSE"/. "$BUILD_DIR/wheelhouse/"
cp -a "$HANDLERS_ASSETS"/. "$BUILD_DIR/handlers-assets/"
MERGE_CFG_SRC="$SERVICE_ROOT/scripts/merge_handlers_assets_config.py"
if [[ ! -f "$MERGE_CFG_SRC" ]]; then
  echo "ERROR: Missing $MERGE_CFG_SRC" >&2
  exit 1
fi
cp "$MERGE_CFG_SRC" "$BUILD_DIR/merge_handlers_assets_config.py"

case "$BUILD_DIR" in
  "$WORKSPACE_ROOT"/*) BUILD_REL="${BUILD_DIR#"$WORKSPACE_ROOT"/}" ;;
  *)
    echo "ERROR: BUILD_DIR is not under WORKSPACE_ROOT ($BUILD_DIR vs $WORKSPACE_ROOT)" >&2
    echo "OUTPUT_STATE_DIR must stay inside the Docker context." >&2
    exit 1
    ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed or not in PATH" >&2
  exit 1
fi

echo "==> Using Docker to build Lambda-compatible package..."
echo "    Service tooling staged at: $BUILD_REL/"
echo "    Wheelhouse mode: packages=$HANDLERS_PACKAGES"
echo ""

RUN_ZIP_LINE='    zip -r /build/lambda_deployment.zip . -q && \'
PACKAGES_SPACED="${HANDLERS_PACKAGES//,/ }"

cat > "$BUILD_DIR/Dockerfile" << DOCKERFILE
FROM public.ecr.aws/lambda/python:3.12

RUN microdnf install -y zip && microdnf clean all

RUN python3.12 -m pip install --upgrade pip setuptools wheel && \\
    pip install "PyYAML>=6.0" && \\
    python3.12 -c "import yaml; print('PyYAML installed in image, version:', yaml.__version__)"

WORKDIR /build

COPY ${BUILD_REL}/wheelhouse/ /build/wheelhouse/
COPY ${BUILD_REL}/handlers-assets/ /build/handlers-assets/
COPY ${BUILD_REL}/merge_handlers_assets_config.py /build/merge_handlers_assets_config.py
COPY ${RENGLO_LIB_COPY_PATH}/ /build/renglo-lib/
RUN set -e && \\
    cd /build && \\
    python3.12 -m pip install --upgrade pip setuptools wheel -q && \\
    mkdir -p /build/output && \\
    python3.12 -m pip install --no-cache-dir --target /build/output /build/renglo-lib && \\
    python3.12 -m pip install --no-cache-dir --no-index --find-links=/build/wheelhouse --target /build/output ${PACKAGES_SPACED} && \\
    cp /build/handlers-assets/lambda_router.py /build/output/ && \\
    cp /build/handlers-assets/handlers_config.json /build/output/ && \\
    python3.12 /build/merge_handlers_assets_config.py && \\
    python3.12 -c "import sys; sys.path.insert(0, '/build/output'); import yaml; print('✓ yaml OK')" && \\
    cd /build/output && \\
    find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type f -name '*.pyc' -delete 2>/dev/null || true && \\
    find . -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true && \\
    find . -type f -name '*.md' -delete 2>/dev/null || true && \\
    find . -type d -name 'examples' -exec rm -rf {} + 2>/dev/null || true && \\
    $RUN_ZIP_LINE
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
  docker_cli run --rm \
    --platform "$DOCKER_PLATFORM" \
    --entrypoint /bin/sh \
    -v "$(docker_volume_host_path "$OUTPUT_STATE_DIR"):/output" \
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
  echo "Primary env: $EXTENSION_NAME"
  echo "Packages:        $HANDLERS_PACKAGES"
  echo ""
else
  echo "==> Skipping zip extraction (local-only image)"
  echo ""
  echo "=========================================="
  echo "Build complete!"
  echo "=========================================="
  echo "Image: $DOCKER_IMAGE"
  echo "Primary env: $EXTENSION_NAME"
  echo "Packages:        $HANDLERS_PACKAGES"
  echo "(use with run-local and EXTENSION_SERVICE_NATIVE_PLATFORM=1)"
  echo ""
fi

rm -rf "$BUILD_DIR"
