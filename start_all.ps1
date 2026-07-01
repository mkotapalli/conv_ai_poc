[CmdletBinding()]
param(
	[ValidateSet('company', 'personal')]
	[string]$Account = 'company',

	[string]$CompanyProfile = 'default',

	[string]$PersonalProfile = 'personal',

	[string]$Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { 'us-east-1' })
)

$awsProfile = if ($Account -eq 'personal') { $PersonalProfile } else { $CompanyProfile }
$env:AWS_PROFILE = $awsProfile
$env:AWS_REGION = $Region

$bootstrap = "`$env:AWS_REGION='$Region'; `$env:AWS_PROFILE='$awsProfile';"
if ($env:AWS_ACCESS_KEY_ID) { $bootstrap += " `$env:AWS_ACCESS_KEY_ID='$($env:AWS_ACCESS_KEY_ID)';" }
if ($env:AWS_SECRET_ACCESS_KEY) { $bootstrap += " `$env:AWS_SECRET_ACCESS_KEY='$($env:AWS_SECRET_ACCESS_KEY)';" }
if ($env:AWS_SESSION_TOKEN) { $bootstrap += " `$env:AWS_SESSION_TOKEN='$($env:AWS_SESSION_TOKEN)';" }

Write-Host "Starting local services with AWS account '$Account', profile '$awsProfile', region '$Region'." -ForegroundColor Cyan

Start-Process powershell -ArgumentList '-NoExit', '-Command', "$bootstrap cd c:\projects\gen_agent_ai; `$env:SERVICE_ACCOUNT_AGENT_INVOCATIONS_URL='http://localhost:8082/invocations'; c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8081 --app-dir src/orchestrator-agent"
Start-Process powershell -ArgumentList '-NoExit', '-Command', "$bootstrap cd c:\projects\gen_agent_ai\src\acct-mgmt-agent; `$env:SERVER_PORT='8082'; `$env:MCP_SERVER_PORT='8083'; c:/projects/gen_agent_ai/.venv/Scripts/python.exe dual_server.py"

Write-Host "Started orchestrator-agent and merged acct-mgmt-agent (REST + MCP)." -ForegroundColor Green
Write-Host "Use './start_all.ps1 -Account company' for company AWS or './start_all.ps1 -Account personal' for personal AWS." -ForegroundColor Green
Write-Host "REST Agent: http://localhost:8082/health" -ForegroundColor Yellow
Write-Host "MCP Server: http://localhost:8083/health" -ForegroundColor Yellow
