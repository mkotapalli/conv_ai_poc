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
Start-Process powershell -ArgumentList '-NoExit', '-Command', "$bootstrap cd c:\projects\gen_agent_ai; `$env:SERVICE_ACCOUNT_API_MCP_URL='http://localhost:8084/mcp'; c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8082 --app-dir src/acct-mgmt-agent"
Start-Process powershell -ArgumentList '-NoExit', '-Command', "$bootstrap cd c:\projects\gen_agent_ai; `$env:SERVICE_ORCHESTRATOR_URL='http://localhost:8081/invocations'; c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8083 --app-dir src/acct-mgnt-mcp"
Start-Process powershell -ArgumentList '-NoExit', '-Command', "$bootstrap cd c:\projects\gen_agent_ai; c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8084 --app-dir src/account-api-mcp"

Write-Host "Started orchestrator-agent, acct-mgmt-agent, acct-mgnt-mcp, and account-api-mcp." -ForegroundColor Green
Write-Host "Use './start_all.ps1 -Account company' for company AWS or './start_all.ps1 -Account personal' for personal AWS." -ForegroundColor Green
Write-Host "MCP Server: http://localhost:8083/health" -ForegroundColor Yellow
Write-Host "Account API MCP: http://localhost:8084/health" -ForegroundColor Yellow
