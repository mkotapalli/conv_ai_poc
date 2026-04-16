# AgentCore POC for Account Access

This repository contains a **3-service FastAPI proof of concept** for an AWS-hosted conversation AI flow:

- `src/orchestrator-agent` – uses **Strands SDK** plus **Bedrock model configuration** to classify intent and route password requests, using **mandatory Strands A2A handoff** to the account-management agent.
- `src/acct-mgmt-agent` – uses **Strands SDK** plus **Bedrock model configuration** to answer password reset/unlock prompts.
- `src/acct-mgnt-mcp` – **MCP (Model Context Protocol) server** that bridges AWS AgentCore Gateway to the orchestrator, enabling tool/resource invocation with SigV4 authentication.

## Key design points

- **FastAPI everywhere**
- **Properties-file driven configuration** for easy environment promotion
- **Mandatory Strands A2A orchestration** from `orchestrator-agent` to `acct-mgmt-agent`
- **SigV4 protection** for orchestrator/account-agent and MCP-facing service traffic
- **AgentCore-style memory abstraction** with local fallback for laptop development
- **Dedicated Dockerfile and requirements** per component for independent deployment

## Local run

### 1. Start with Docker Compose

```bash
docker compose up --build
```

Starts 3 services:
- Orchestrator: http://localhost:8081
- Account Agent: http://localhost:8082
- MCP Server: http://localhost:8083

### 2. Or run the services manually

Use the startup script with account selection:
```bash
./start_all.ps1 -Account company          # or -Account personal
```

Or run individually:
```bash
c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8081 --app-dir src/orchestrator-agent
c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8082 --app-dir src/acct-mgmt-agent
c:/projects/gen_agent_ai/.venv/Scripts/python.exe -m uvicorn main:app --host 0.0.0.0 --port 8083 --app-dir src/acct-mgnt-mcp
```

### 3. Smoke test

```bash
c:/projects/gen_agent_ai/.venv/Scripts/python.exe smoke_test.py
```

## Deployment notes

- Update each `config/application.properties` file with your **AWS region**, **Bedrock model ID**, **Okta issuer/audience/JWKS**, **Guardrail ID/version**, and service URLs.
- Set `bedrock.guardrail.enabled=true` plus `bedrock.guardrail_id=<your-guardrail-id>` in both agent properties files to enable **AWS Guardrails**.
- For production, place `orchestrator-agent` and `acct-mgnt-mcp` behind API Gateway / ALB and enforce IAM-authenticated SigV4 on the AWS side.
- The memory abstraction is intentionally **POC-safe**: it runs locally today and can be swapped to a managed AgentCore memory provider later.
