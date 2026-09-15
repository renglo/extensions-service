#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

# Build handlers images from prepare_handlers_wheelhouse.py output.
# Requires: EXTENSION_NAME, WORKSPACE_ROOT, HANDLERS_WHEELHOUSE, HANDLERS_ASSETS,
# HANDLERS_PACKAGES (comma-separated dist names, including renglo-lib when pinned).
# Optional: OUTPUT_STATE_DIR, DEPLOYMENT_ZIP,
#           EXTENSION_SERVICE_LARGE_BUILD=1 for ECS/*-ecs-builder (no zip).
#
# All packages (including renglo-lib) install from the wheelhouse — no git clone
# of dev/renglo-lib. Large uses the same wheelhouse; prepare with --with-large-deps
# so name[large-dependencies] wheels are present. AWS async/batch uses this image's
# ecs_handler_entrypoint.py. Local Docker async/batch is out of scope.

if [[ -z "${EXTENSION_NAME:-}" || -z "${WORKSPACE_ROOT:-}" ]]; then
  echo "ERROR: EXTENSION_NAME and WORKSPACE_ROOT must be set (run via: python run.py <env> build)" >&2
  exit 1
fi

HANDLERS_WHEELHOUSE="${HANDLERS_WHEELHOUSE:-}"
HANDLERS_ASSETS="${HANDLERS_ASSETS:-}"
HANDLERS_PACKAGES="${HANDLERS_PACKAGES:-}"
BUILD_LARGE="${EXTENSION_SERVICE_LARGE_BUILD:-0}"

WORKSPACE_ROOT="$(cd "$WORKSPACE_ROOT" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -z "$HANDLERS_WHEELHOUSE" || -z "$HANDLERS_ASSETS" || -z "$HANDLERS_PACKAGES" ]]; then
  echo "ERROR: handlers build requires a package wheelhouse." >&2
  echo "  Prepare: python ops/bom-helper/scripts/prepare_handlers_wheelhouse.py \\" >&2
  echo "             --from-monorepo extensions/<ext>/package,... [--with-large-deps] --out .handlers-build" >&2
  echo "  Build:   python ops/extensions-service/run.py <env> build [--large] \\" >&2
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

if [[ "$BUILD_LARGE" == "1" ]]; then
  IMAGE_KIND="ecs-builder"
else
  IMAGE_KIND="lambda-builder"
fi

if [[ "${EXTENSION_SERVICE_NATIVE_PLATFORM:-0}" == "1" ]]; then
  DOCKER_PLATFORM="linux/arm64"
  DOCKER_IMAGE="${EXTENSION_NAME}-${IMAGE_KIND}:local"
  EXTRACT_ZIP=false
  echo "=========================================="
  echo "Building ${IMAGE_KIND} local-only image (arm64): $EXTENSION_NAME"
  echo "=========================================="
else
  DOCKER_PLATFORM="linux/amd64"
  DOCKER_IMAGE="${EXTENSION_NAME}-${IMAGE_KIND}:latest"
  if [[ "$BUILD_LARGE" == "1" ]]; then
    EXTRACT_ZIP=false
  else
    EXTRACT_ZIP=true
  fi
  echo "=========================================="
  if [[ "$BUILD_LARGE" == "1" ]]; then
    echo "Building ECS handlers image (amd64): $EXTENSION_NAME"
  else
    echo "Building Lambda Deployment Package (amd64): $EXTENSION_NAME"
  fi
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

ENTRYPOINT_SRC="$SERVICE_ROOT/scripts/ecs_handler_entrypoint.py"
INSTALL_LARGE_SRC="$SERVICE_ROOT/scripts/install_large_extras.py"
CORE_PACKAGES_SRC="$SERVICE_ROOT/scripts/handlers_core_packages.py"
WHEEL_LIBS_SRC="$SERVICE_ROOT/wheel_libs.json"
if [[ "$BUILD_LARGE" == "1" ]]; then
  if [[ ! -f "$ENTRYPOINT_SRC" ]]; then
    echo "ERROR: Missing $ENTRYPOINT_SRC" >&2
    exit 1
  fi
  if [[ ! -f "$INSTALL_LARGE_SRC" || ! -f "$CORE_PACKAGES_SRC" ]]; then
    echo "ERROR: Missing $INSTALL_LARGE_SRC or $CORE_PACKAGES_SRC" >&2
    exit 1
  fi
  cp "$ENTRYPOINT_SRC" "$BUILD_DIR/ecs_handler_entrypoint.py"
  cp "$INSTALL_LARGE_SRC" "$BUILD_DIR/install_large_extras.py"
  cp "$CORE_PACKAGES_SRC" "$BUILD_DIR/handlers_core_packages.py"
  if [[ -f "$WHEEL_LIBS_SRC" ]]; then
    cp "$WHEEL_LIBS_SRC" "$BUILD_DIR/wheel_libs.json"
  else
    echo '[]' > "$BUILD_DIR/wheel_libs.json"
  fi
fi

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

echo "==> Using Docker to build handlers package..."
echo "    Service tooling staged at: $BUILD_REL/"
echo "    Wheelhouse mode: packages=$HANDLERS_PACKAGES large=$BUILD_LARGE"
echo ""

PACKAGES_SPACED="${HANDLERS_PACKAGES//,/ }"

if [[ "$BUILD_LARGE" == "1" ]]; then
  FINAL_LINES='cp /build/ecs_handler_entrypoint.py /build/output/ecs_handler_entrypoint.py; echo "ECS image build complete!"'
  DOCKER_TAIL='WORKDIR /build/output
ENTRYPOINT ["python3.12", "/build/output/ecs_handler_entrypoint.py"]'
  EXTRA_COPY="COPY ${BUILD_REL}/ecs_handler_entrypoint.py /build/ecs_handler_entrypoint.py
COPY ${BUILD_REL}/install_large_extras.py /build/install_large_extras.py
COPY ${BUILD_REL}/handlers_core_packages.py /build/handlers_core_packages.py
COPY ${BUILD_REL}/wheel_libs.json /build/wheel_libs.json"
  LARGE_INSTALL_LINE="    HANDLERS_PACKAGES=${HANDLERS_PACKAGES} python3.12 /build/install_large_extras.py; \\"
else
  FINAL_LINES='zip -r /build/lambda_deployment.zip . -q; echo "Build complete!"'
  DOCKER_TAIL=''
  EXTRA_COPY=''
  LARGE_INSTALL_LINE="    :; \\"
fi

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
${EXTRA_COPY}
RUN set -eux; \\
    cd /build; \\
    python3.12 -m pip install --upgrade pip setuptools wheel -q; \\
    mkdir -p /build/output; \\
    python3.12 -m pip install --no-cache-dir --no-index --find-links=/build/wheelhouse --target /build/output ${PACKAGES_SPACED}; \\
${LARGE_INSTALL_LINE}
    cp /build/handlers-assets/lambda_router.py /build/output/; \\
    cp /build/handlers-assets/handlers_config.json /build/output/; \\
    python3.12 /build/merge_handlers_assets_config.py; \\
    python3.12 -c "import sys; sys.path.insert(0, '/build/output'); import yaml; print('OK yaml')"; \\
    cd /build/output; \\
    find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true; \\
    find . -type f -name '*.pyc' -delete 2>/dev/null || true; \\
    find . -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true; \\
    find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true; \\
    find . -type f -name '*.md' -delete 2>/dev/null || true; \\
    find . -type d -name 'examples' -exec rm -rf {} + 2>/dev/null || true; \\
    ${FINAL_LINES}
${DOCKER_TAIL}
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
  echo "==> Skipping zip extraction (${IMAGE_KIND} image)"
  echo ""
  echo "=========================================="
  echo "Build complete!"
  echo "=========================================="
  echo "Image: $DOCKER_IMAGE"
  echo "Primary env: $EXTENSION_NAME"
  echo "Packages:        $HANDLERS_PACKAGES"
  if [[ "$BUILD_LARGE" == "1" ]]; then
    echo "(ECS image; AWS async uses entrypoint. Local smoke = sync invoke.)"
  else
    echo "(use with run-local and EXTENSION_SERVICE_NATIVE_PLATFORM=1)"
  fi
  echo ""
fi

rm -rf "$BUILD_DIR"
