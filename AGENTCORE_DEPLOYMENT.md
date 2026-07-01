# AgentCore Deployment

## Architecture

```
Client (LLM / GCP / Portal)
   │
   ▼
AgentCore Gateway (Okta-protected, MCP)
   │  SigV4
   ▼
acct-mgmt-agent Runtime  (REST on 8080, MCP on 8000)
```

Runtime-to-runtime communication uses SigV4-signed HTTPS to the AgentCore
invocations endpoint. No VPC networking is required.

---

## Current Runtime IDs (bcbs-dev)

| Component | Runtime ID | Role suffix |
|---|---|---|
| Orchestrator | `bcbs_dev_convai_orchestrator-GEkw1YGeCY` | `-kugos` |
| Acct-mgmt | `bcbs_dev_convai_acct_mgnt-8xi3SACIES` | *(separate role)* |

---

## 1. Build and Push ARM64 Images

```powershell
.\build_and_push_to_ecr.ps1 -ImageTag latest
```

Linux / bastion:

```bash
chmod +x ./build_and_push_to_ecr.sh
./build_and_push_to_ecr.sh --image-tag latest
```

ECR images produced:

| Tag | Runtime |
|---|---|
| `orchestrator-agent-latest` | orchestrator-agent |
| `acct-mgmt-agent-latest` | acct-mgmt-agent |

ECR registry: `834458830002.dkr.ecr.us-east-1.amazonaws.com/aie_account_management_svc`

---

## 2. Create / Update Runtime: acct-mgmt-agent

| Field | Value |
|---|---|
| Protocol | `HTTP` and `MCP` listeners in same image |
| Port | `8080` |
| Image | `...:acct-mgmt-agent-latest` |

Environment variables:

```
AWS_REGION=us-east-1
AWS_SECRETSMANAGER_ENABLED=true
AWS_SECRETSMANAGER_SECRET_NAME=bcbs-dev-convai-secrets
MEMORY_AGENTCORE_MEMORY_ID=bcbs_dev_convai_memory-KBvo716q7d
MCP_SERVER_PORT=8000
SERVER_PORT=8080
```

- No downstream runtime calls — does not need `InvokeRuntime` permission.
- Exposes `POST /invocations` and `POST /assist` on 8080.
- Exposes MCP streamable HTTP on `/mcp` at 8000.

---

## 3. Create / Update Runtime: orchestrator-agent

| Field | Value |
|---|---|
| Protocol | `HTTP` |
| Port | `8080` |
| Image | `...:orchestrator-agent-latest` |

Environment variables:

```
AWS_REGION=us-east-1
AWS_SECRETSMANAGER_ENABLED=true
AWS_SECRETSMANAGER_SECRET_NAME=bcbs-dev-convai-secrets
MEMORY_AGENTCORE_MEMORY_ID=bcbs_dev_convai_memory-KBvo716q7d
SERVICE_ACCOUNT_AGENT_INVOCATIONS_URL=https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A834458830002%3Aruntime%2Fbcbs_dev_convai_acct_mgnt-8xi3SACIES/invocations?qualifier=bcbs_dev_acct_mgmt_endpoint
```

- `SERVICE_ACCOUNT_AGENT_INVOCATIONS_URL` must be the fully URL-encoded AgentCore invocations URL for acct-mgmt-agent.
- Exposes `POST /invocations` and `POST /orchestrate`.

---

## 4. Gateway MCP target

- Point the gateway MCP integration directly to the acct-mgmt-agent MCP endpoint (port 8000, path `/mcp`).
- No standalone `acct-mgnt-mcp` runtime is required.

---

## 5. IAM Execution Policies

Each runtime role needs cross-runtime `InvokeRuntime` permission on the downstream runtime.
See [AGENTCORE_EXECUTION_POLICIES.md](AGENTCORE_EXECUTION_POLICIES.md) for full policy documents and CLI commands.

Summary of inline policies to add:

| Role (suffix) | Policy name | Allows |
|---|---|---|
| `-kugos` (Orchestrator) | `AllowInvokeAcctRuntime` | `InvokeRuntime` on acct-mgmt ARN |

Apply:

```powershell
# Orchestrator role -> acct-mgmt
aws iam put-role-policy `
  --role-name "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-kugos" `
  --policy-name "AllowInvokeAcctRuntime" `
  --policy-document file://iam/allow-invoke-acct.json
```

> **Cleanup**: Remove any `TempAgentCoreFullAccess` (`bedrock-agentcore:*`) inline policies added during debugging.

---

## 6. Configure Gateway

1. AgentCore → Gateways → open `bcbs-dev-convai-gateway`
2. MCP target must point at the MCP runtime endpoint.
3. Gateway URL: `https://bcbs-dev-convai-gateway-e4ing1lwcu.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp`

Okta integration is configured at the gateway layer. Clients must supply a valid Okta bearer token.

---

## 7. Validation

### Per-runtime health

```powershell
# Direct invocation test (SigV4 signed)
python tests\test_runtime_invocation.py `
  --url "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A834458830002%3Aruntime%2Fbcbs_dev_convai_orchestrator-GEkw1YGeCY/invocations?qualifier=bcbs_dev_orchestrator_endpoint" `
  --message "I need to reset my password"
```

### End-to-end through gateway (requires Okta token)

```powershell
python tests\request_gateway.py --call-sample --sample-intent Account_Unlock
python tests\request_gateway.py --call-sample --sample-intent Account_PW_Reset
```

### End-to-end bypassing gateway/Okta (SigV4 direct to MCP runtime)

```powershell
python tests\request_mcp_direct.py `
  --url "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/<mcp-runtime-invocations-url>/mcp" `
  --call-sample --sample-intent Account_Unlock
```

### Expected response shape

```json
{
  "intent": "Account_Unlock",
  "conversation_id": "...",
  "account_found": "yes",
  "account_locked_at_start": "yes",
  "account_locked_at_end": "no",
  "password_reset": "no",
  "action_detail": [{"action": "unlock_account", "status": "success"}],
  "delivery_mode": "sigv4_invocation"
}
```

---

## 8. Local Development

Start all services locally:

```powershell
.\start_all.ps1
```

Service ports:

| Service | Port |
|---|---|
| acct-mgmt-agent | 8082 |
| orchestrator-agent | 8081 |
| acct-mgmt-agent (MCP) | 8083 |

Run MCP tests locally:

```powershell
python tests\test_mcp_server.py
python tests\request_mcp_direct.py --url http://localhost:8083/mcp --call-sample
```

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 Forbidden` from AgentCore invocations URL | Missing `InvokeRuntime` IAM permission | Add inline policy per section 5 |
| `424` from AgentCore | Container returned non-2xx (missing `/invocations` route or health probe failure) | Verify `/ping` and `/invocations` routes exist; check container logs |
| `status: skipped` in orchestrator response | `SERVICE_ACCOUNT_AGENT_INVOCATIONS_URL` env var not set on orchestrator runtime | Set env var in AgentCore console and restart runtime |
| `status: degraded` in orchestrator response | Acct-mgmt runtime unreachable or returned error | Check acct-mgmt runtime health and IAM permissions |
| MCP `406 Not Acceptable` | Missing `Accept` header | Send `Accept: application/json, text/event-stream` |
| Gateway 403 (not IAM) | Missing or invalid Okta token | Use `request_mcp_direct.py` to bypass gateway and test MCP runtime directly |
