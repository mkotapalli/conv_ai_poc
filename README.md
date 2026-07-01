# AgentCore POC for Account Access

This repository contains a **2-service FastAPI proof of concept** for account recovery with stubbed API behavior:

- `src/orchestrator-agent` – legacy routing layer for compatibility and session handoff.
- `src/acct-mgmt-agent` – owns `aie_session_id`, returns stubbed account validation and password-link responses, exposes REST endpoints, and also hosts MCP on a second port.

## Supported request types

- `validate_account`
- `pw_send_link`
- `end_session`

Expected key fields are:

- `genesys_conversation_id`
- `gecx_session_id`
- `request_type`
- `member_eid` (for validate/link)
- `delivery_type` (sms/email for link)
- `aie_session_id` (created by acct-mgmt-agent during validate_account and reused for link/end_session)

## Local run

### 1. Start with Docker Compose

```bash
docker compose up --build
```

Starts 2 services:
- Orchestrator: http://localhost:8081
- Account Agent REST: http://localhost:8082
- Account Agent MCP: http://localhost:8083

### 2. Or run the services manually

Use the startup script with account selection:
```bash
./start_all.ps1 -Account company          # or -Account personal
```

Or run individually:
```bash
c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8081 --app-dir src/orchestrator-agent
c:/projects/gen_agent_ai/.venv/Scripts/python.exe src/acct-mgmt-agent/dual_server.py
```

### 3. Smoke test

```bash
c:/projects/gen_agent_ai/.venv/Scripts/python.exe smoke_test.py
```

## Deployment notes

- Update each `config/application.properties` file with your AWS region, secrets manager settings, and service URLs.
- For production, place `orchestrator-agent` and `acct-mgmt-agent` behind API Gateway / ALB and enforce IAM authentication at the UI/gateway layer.
- From a Linux bastion host, build and push images with `./build_and_push_to_ecr.sh --image-tag latest`.
- Session state is persisted by orchestrator under local data storage and keyed by `gecx_session_id`.
