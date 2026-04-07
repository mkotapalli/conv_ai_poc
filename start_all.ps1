$awsRegion = if ($env:AWS_REGION) { $env:AWS_REGION } else { 'us-east-1' }
$awsProfile = if ($env:AWS_PROFILE) { $env:AWS_PROFILE } else { 'default' }

$bootstrap = "`$env:AWS_REGION='$awsRegion'; `$env:AWS_PROFILE='$awsProfile';"
if ($env:AWS_ACCESS_KEY_ID) { $bootstrap += " `$env:AWS_ACCESS_KEY_ID='$($env:AWS_ACCESS_KEY_ID)';" }
if ($env:AWS_SECRET_ACCESS_KEY) { $bootstrap += " `$env:AWS_SECRET_ACCESS_KEY='$($env:AWS_SECRET_ACCESS_KEY)';" }
if ($env:AWS_SESSION_TOKEN) { $bootstrap += " `$env:AWS_SESSION_TOKEN='$($env:AWS_SESSION_TOKEN)';" }

Start-Process powershell -ArgumentList '-NoExit', '-Command', "$bootstrap cd c:\projects\gen_agent_ai; c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir src/gateway"
Start-Process powershell -ArgumentList '-NoExit', '-Command', "$bootstrap cd c:\projects\gen_agent_ai; c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8081 --app-dir src/orchestrator-agent"
Start-Process powershell -ArgumentList '-NoExit', '-Command', "$bootstrap cd c:\projects\gen_agent_ai; c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8082 --app-dir src/acct-mgmt-agent"
Write-Host "Started gateway, orchestrator-agent, and acct-mgmt-agent with AWS environment inheritance." -ForegroundColor Green
