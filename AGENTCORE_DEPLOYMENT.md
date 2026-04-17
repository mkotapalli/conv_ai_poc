# AgentCore Deployment (Separate Runtimes + Strands Multi-Agent A2A)

This setup uses separate runtime images and explicit Strands A2A server/client with agent card.

This is the only supported orchestrator-to-account-agent path in the current codebase. HTTP fallback and in-process tool-handoff fallback have been removed.

## Architecture

```
Client
   -> AgentCore Gateway
   -> acct-mgnt-mcp Runtime (MCP)
   -> orchestrator-agent Runtime (Strands A2A client)
   -> acct-mgmt-agent Runtime (Strands A2A server + agent card)
```

## 1. Build and Push ARM64 Images

```powershell
.\build_and_push_to_ecr.ps1 -ImageTag latest
```

Linux bastion host:

```bash
chmod +x ./build_and_push_to_ecr.sh
./build_and_push_to_ecr.sh --image-tag latest
```

Required runtime images:

- `834458830002.dkr.ecr.us-east-1.amazonaws.com/aie_account_management_svc:orchestrator-agent-latest`
- `834458830002.dkr.ecr.us-east-1.amazonaws.com/aie_account_management_svc:acct-mgmt-agent-latest`
- `834458830002.dkr.ecr.us-east-1.amazonaws.com/aie_account_management_svc:acct-mgnt-mcp-latest`

## 2. Create Runtime in AWS Console: acct-mgmt-agent (A2A Server)

1. AgentCore -> Runtimes -> Create runtime
2. Name: `acct-mgmt-agent-runtime`
3. Source: Container image
4. Image URI: `...:acct-mgmt-agent-latest`
5. Protocol: `HTTP`
6. Port: `8082`
7. Add VPC + IAM runtime role
8. Add environment variables:
    - `AWS_REGION=us-east-1`
    - `AWS_SECRETSMANAGER_ENABLED=true`
    - `AWS_SECRETSMANAGER_SECRET_NAME=bcbs-dev-convai-secrets`
    - `MEMORY_AGENTCORE_MEMORY_ID=bcbs_dev_convai_memory-KBvo716q7d`
    - `SERVICE_A2A_ENABLED=true`
    - `SERVICE_A2A_MOUNT_PATH=/a2a`
    - `SERVICE_A2A_PUBLIC_URL=<ACCT_MGMT_ENDPOINT_URL>/a2a`
    - `AUTH_A2A_REQUIRED_HEADER=x-agentcore-a2a`
    - `AUTH_A2A_EXPECTED_VALUE=<YOUR_SHARED_A2A_TOKEN>`
9. Create endpoint and save as `<ACCT_MGMT_ENDPOINT_URL>`

Notes:

- A2A agent card URL will be available at: `<ACCT_MGMT_ENDPOINT_URL>/a2a/.well-known/agent-card.json`
- A2A JSON-RPC endpoint will be: `<ACCT_MGMT_ENDPOINT_URL>/a2a`

## 3. Create Runtime in AWS Console: orchestrator-agent (A2A Client)

1. AgentCore -> Runtimes -> Create runtime
2. Name: `orchestrator-agent-runtime`
3. Source: Container image
4. Image URI: `...:orchestrator-agent-latest`
5. Protocol: `HTTP`
6. Port: `8081`
7. Add VPC + IAM runtime role
8. Add environment variables:
    - `AWS_REGION=us-east-1`
    - `AWS_SECRETSMANAGER_ENABLED=true`
    - `AWS_SECRETSMANAGER_SECRET_NAME=bcbs-dev-convai-secrets`
    - `MEMORY_AGENTCORE_MEMORY_ID=bcbs_dev_convai_memory-KBvo716q7d`
    - `SERVICE_ACCOUNT_AGENT_INVOKE_MODE=a2a`
    - `SERVICE_ACCOUNT_AGENT_A2A_URL=<ACCT_MGMT_ENDPOINT_URL>/a2a`
9. Create endpoint and save as `<ORCHESTRATOR_ENDPOINT_URL>`

## 4. Create Runtime in AWS Console: acct-mgnt-mcp

1. AgentCore -> Runtimes -> Create runtime
2. Name: `acct-mgnt-mcp-runtime`
3. Source: Container image
4. Image URI: `...:acct-mgnt-mcp-latest`
5. Protocol: `MCP`
6. Port: `8083`
7. Add VPC + IAM runtime role
8. Add environment variables:
    - `AWS_REGION=us-east-1`
    - `AWS_SECRETSMANAGER_ENABLED=true`
    - `AWS_SECRETSMANAGER_SECRET_NAME=bcbs-dev-convai-secrets`
    - `MEMORY_AGENTCORE_MEMORY_ID=bcbs_dev_convai_memory-KBvo716q7d`
    - `SERVICE_ORCHESTRATOR_URL=<ORCHESTRATOR_ENDPOINT_URL>/orchestrate`
9. Create endpoint and save as `<MCP_ENDPOINT_URL>`

## 5. Configure Gateway

1. AgentCore -> Gateways -> Create gateway
2. Attach memory ID: `bcbs_dev_convai_memory-KBvo716q7d`
3. Add MCP integration:
    - Target URL: `<MCP_ENDPOINT_URL>`
    - Tool: `orchestrator_invoke`
4. Save `<GATEWAY_INVOKE_URL>`

## 6. Validation

- `GET <ACCT_MGMT_ENDPOINT_URL>/a2a/.well-known/agent-card.json` returns an agent card
- `GET <ORCHESTRATOR_ENDPOINT_URL>/health` shows:
   - `account_agent_invoke_mode=a2a`
   - `account_agent_a2a_url=<ACCT_MGMT_ENDPOINT_URL>/a2a`
- End-to-end response includes:
   - `routed_to=acct-mgmt-agent`
   - `delivery_mode=strands_multiagent_a2a`

Local validation before AgentCore deployment:

- Start services locally with [start_all.ps1](start_all.ps1)
- Verify account-agent card locally at `http://localhost:8082/a2a/.well-known/agent-card.json`
- Verify orchestrator health locally at `http://localhost:8081/health`
- Run [tests/test_mcp_server.py](tests/test_mcp_server.py) against local MCP on `http://localhost:8083`

## 7. Troubleshooting

- `ModuleNotFoundError: No module named a2a`:
   - Ensure image was built after adding `a2a-sdk` dependency.
- A2A card fetch fails:
   - Verify account-agent runtime endpoint and `/a2a/.well-known/agent-card.json` path.
- Auth failures calling account-agent:
   - Verify `AUTH_A2A_REQUIRED_HEADER` and `AUTH_A2A_EXPECTED_VALUE`.
- Unexpected health output or delivery mode:
   - Confirm `service.account_agent.invoke_mode=a2a` and `service.account_agent.a2a_url` are the only downstream account-agent settings in [src/orchestrator-agent/config/application.properties](src/orchestrator-agent/config/application.properties).
- MCP 406:
   - Send `Accept: application/json, text/event-stream`.
