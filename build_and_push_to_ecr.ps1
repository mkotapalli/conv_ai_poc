[CmdletBinding()]
param(
    [string]$AWSAccountId = "834458830002",
    [string]$AWSRegion = "us-east-1",
    [string]$RepositoryName = "aie_account_management_svc",
    [string]$ImageTag = "latest",
    [switch]$CleanupDanglingImages = $false
)

$ECRRegistry = "$AWSAccountId.dkr.ecr.$AWSRegion.amazonaws.com"
$Platform = "linux/arm64"

# Services to build and push - each gets its own image with unique tag
$Services = @(
    @{ Name = "orchestrator-agent"; Path = "src/orchestrator-agent"; ImageTag = "orchestrator-agent-$ImageTag" },
    @{ Name = "acct-mgmt-agent"; Path = "src/acct-mgmt-agent"; ImageTag = "acct-mgmt-agent-$ImageTag" },
    @{ Name = "acct-mgnt-mcp"; Path = "src/acct-mgnt-mcp"; ImageTag = "acct-mgnt-mcp-$ImageTag" }
)

Write-Host "Building and pushing individual images to ECR" -ForegroundColor Cyan
Write-Host "Registry: $ECRRegistry" -ForegroundColor Cyan
Write-Host "Repository: $RepositoryName" -ForegroundColor Cyan
Write-Host "Base Tag: $ImageTag" -ForegroundColor Cyan
Write-Host "Architecture: arm64 (forced for AgentCore)" -ForegroundColor Cyan
Write-Host "Region: $AWSRegion" -ForegroundColor Cyan
Write-Host ""

# Verify ECR repository exists
Write-Host "Checking ECR repository: $RepositoryName..." -ForegroundColor Yellow
aws ecr describe-repositories --repository-names $RepositoryName --region $AWSRegion 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Repository '$RepositoryName' does not exist" -ForegroundColor Red
    Write-Host "Please create it in AWS ECR Console first" -ForegroundColor Red
    exit 1
} else {
    Write-Host "Repository found!" -ForegroundColor Green
}
Write-Host ""

# ARM64-only build requires buildx
Write-Host "Checking docker buildx availability for ARM64 build..." -ForegroundColor Yellow
docker buildx ls 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: docker buildx is required for ARM64 builds" -ForegroundColor Red
    Write-Host "Install/enable buildx and retry" -ForegroundColor Red
    exit 1
}
Write-Host "docker buildx available - will build for linux/arm64" -ForegroundColor Green

Write-Host ""

foreach ($Service in $Services) {
    $ServiceName = $Service.Name
    $ServicePath = $Service.Path
    $ServiceImageTag = $Service.ImageTag
    $ImageUri = "$ECRRegistry/${RepositoryName}:${ServiceImageTag}"

    Write-Host "========================================" -ForegroundColor Green
    Write-Host "Processing: $ServiceName" -ForegroundColor Green
    Write-Host "Path: $ServicePath" -ForegroundColor Green
    Write-Host "Image: $ImageUri" -ForegroundColor Green
    Write-Host "Architecture: arm64" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green

    Write-Host "Building and pushing ARM64 image: $ImageUri..." -ForegroundColor Yellow
    $DockerfileArgs = if ($Service.DockerfilePath) { @("-f", $Service.DockerfilePath) } else { @() }
    docker buildx build --platform $Platform --provenance=false --sbom=false @DockerfileArgs -t $ImageUri --push $ServicePath
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Successfully pushed: $ImageUri" -ForegroundColor Green
    } else {
        Write-Host "ERROR: Failed to build/push $ServiceName" -ForegroundColor Red
        continue
    }

    Write-Host ""
}

# Cleanup dangling images if requested
if ($CleanupDanglingImages) {
    Write-Host "Cleaning up dangling images..." -ForegroundColor Yellow
    docker image prune -f
    Write-Host "Cleanup complete" -ForegroundColor Green
    Write-Host ""
}

Write-Host "========================================" -ForegroundColor Green
Write-Host "All done!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Individual ARM64 images pushed to ECR:" -ForegroundColor Cyan
foreach ($Service in $Services) {
    Write-Host "  - $ECRRegistry/$RepositoryName : $($Service.ImageTag)" -ForegroundColor Cyan
}
Write-Host ""
Write-Host "Use these URIs in separate ECS task definitions:" -ForegroundColor Yellow
foreach ($Service in $Services) {
    Write-Host "  $($Service.Name): $ECRRegistry/$RepositoryName : $($Service.ImageTag)" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "To specify a custom tag, use:" -ForegroundColor Cyan
Write-Host "  ./build_and_push_to_ecr.ps1 -ImageTag v1.0.0" -ForegroundColor Cyan
Write-Host ""
Write-Host "To cleanup dangling images after build, use:" -ForegroundColor Cyan
Write-Host "  ./build_and_push_to_ecr.ps1 -CleanupDanglingImages" -ForegroundColor Cyan
