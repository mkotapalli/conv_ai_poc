#!/usr/bin/env bash

set -euo pipefail

AWS_ACCOUNT_ID="834458830002"
AWS_REGION="us-east-1"
REPOSITORY_NAME="aie_account_management_svc"
IMAGE_TAG="latest"
PLATFORM="linux/arm64"
CLEANUP_DANGLING_IMAGES="false"
SKIP_ECR_LOGIN="false"

usage() {
    cat <<'EOF'
Usage: ./build_and_push_to_ecr.sh [options]

Options:
  --aws-account-id <id>          AWS account ID hosting the ECR repository
  --aws-region <region>          AWS region for ECR operations
  --repository-name <name>       ECR repository name
  --image-tag <tag>              Base image tag to suffix per service image
  --platform <platform>          Docker buildx target platform (default: linux/arm64)
  --cleanup-dangling-images      Run docker image prune -f after the build
  --skip-ecr-login               Skip automatic aws ecr login
  -h, --help                     Show this help message

Examples:
  ./build_and_push_to_ecr.sh --image-tag latest
  ./build_and_push_to_ecr.sh --aws-region us-east-1 --image-tag v1.0.0
EOF
}

require_command() {
    local command_name="$1"
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "ERROR: Required command '$command_name' is not installed or not on PATH." >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --aws-account-id)
            AWS_ACCOUNT_ID="$2"
            shift 2
            ;;
        --aws-region)
            AWS_REGION="$2"
            shift 2
            ;;
        --repository-name)
            REPOSITORY_NAME="$2"
            shift 2
            ;;
        --image-tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        --platform)
            PLATFORM="$2"
            shift 2
            ;;
        --cleanup-dangling-images)
            CLEANUP_DANGLING_IMAGES="true"
            shift
            ;;
        --skip-ecr-login)
            SKIP_ECR_LOGIN="true"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument '$1'." >&2
            usage >&2
            exit 1
            ;;
    esac
done

require_command aws
require_command docker

ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

SERVICES=(
    "orchestrator-agent:src/orchestrator-agent"
    "acct-mgmt-agent:src/acct-mgmt-agent"
    "acct-mgnt-mcp:src/acct-mgnt-mcp"
    "account-api-mcp:src/account-api-mcp"
)

echo "Building and pushing individual images to ECR"
echo "Registry: ${ECR_REGISTRY}"
echo "Repository: ${REPOSITORY_NAME}"
echo "Base Tag: ${IMAGE_TAG}"
echo "Architecture: ${PLATFORM}"
echo "Region: ${AWS_REGION}"
echo

echo "Checking ECR repository: ${REPOSITORY_NAME}..."
if ! aws ecr describe-repositories --repository-names "${REPOSITORY_NAME}" --region "${AWS_REGION}" >/dev/null; then
    echo "ERROR: Repository '${REPOSITORY_NAME}' does not exist." >&2
    echo "Please create it in AWS ECR first." >&2
    exit 1
fi
echo "Repository found!"
echo

if [[ "${SKIP_ECR_LOGIN}" != "true" ]]; then
    echo "Logging in to ECR registry ${ECR_REGISTRY}..."
    aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
    echo "ECR login succeeded."
    echo
fi

echo "Checking docker buildx availability for ${PLATFORM} build..."
if ! docker buildx ls >/dev/null 2>&1; then
    echo "ERROR: docker buildx is required for ${PLATFORM} builds." >&2
    echo "Install or enable buildx and retry." >&2
    exit 1
fi
echo "docker buildx available."
echo

for service in "${SERVICES[@]}"; do
    IFS=':' read -r service_name service_path <<<"${service}"
    image_uri="${ECR_REGISTRY}/${REPOSITORY_NAME}:${service_name}-${IMAGE_TAG}"

    echo "========================================"
    echo "Processing: ${service_name}"
    echo "Path: ${service_path}"
    echo "Image: ${image_uri}"
    echo "Architecture: ${PLATFORM}"
    echo "========================================"

    docker buildx build \
        --platform "${PLATFORM}" \
        --provenance=false \
        --sbom=false \
        -t "${image_uri}" \
        --push \
        "${service_path}"

    echo "Successfully pushed: ${image_uri}"
    echo
done

if [[ "${CLEANUP_DANGLING_IMAGES}" == "true" ]]; then
    echo "Cleaning up dangling images..."
    docker image prune -f
    echo "Cleanup complete"
    echo
fi

echo "========================================"
echo "All done!"
echo "========================================"
echo
echo "Individual ARM64 images pushed to ECR:"
for service in "${SERVICES[@]}"; do
    IFS=':' read -r service_name _ <<<"${service}"
    echo "  - ${ECR_REGISTRY}/${REPOSITORY_NAME}:${service_name}-${IMAGE_TAG}"
done
echo
echo "Use these URIs in separate ECS task definitions:"
for service in "${SERVICES[@]}"; do
    IFS=':' read -r service_name _ <<<"${service}"
    echo "  ${service_name}: ${ECR_REGISTRY}/${REPOSITORY_NAME}:${service_name}-${IMAGE_TAG}"
done
echo
echo "To specify a custom tag, use:"
echo "  ./build_and_push_to_ecr.sh --image-tag v1.0.0"
echo
echo "To cleanup dangling images after build, use:"
echo "  ./build_and_push_to_ecr.sh --cleanup-dangling-images"