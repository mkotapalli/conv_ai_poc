# Account Management MCP Server

The MCP (Model Context Protocol) server bridges AWS AgentCore Gateway to the orchestrator agent by exposing a standards-based MCP tool surface over HTTP. It uses the Python `mcp` server implementation and is packaged so it can run as an ARM64 AgentCore Runtime container.

## Architecture

```
AWS AgentCore Gateway
     ↓
   acct-mgnt-mcp (MCP Server - Port 8083)
     ↓
   acct-mgmt-agent (Port 8082)
```

## Capabilities

### Resources
- **account://password-reset** - Password reset request handling
- **account://account-unlock** - Account unlock request handling

### Tools
- **account_management_invoke** - Forward a user message from AgentCore Gateway directly to `acct-mgmt-agent`
- **orchestrator_invoke** - Backward-compatible alias for `account_management_invoke`

## Configuration

See [config/application.properties](config/application.properties) for all settings:

| Setting | Default | Purpose |
|---------|---------|---------|
| `server.port` | 8083 | MCP server listen port |
| `service.orchestrator.url` | `http://localhost:8081/orchestrate` | Orchestrator endpoint |
| `service.agentcore.runtime.mcp_path` | `/mcp` | AgentCore Runtime MCP endpoint path |
| `aws.secretsmanager.enabled` | true | Load AWS credentials from Secrets Manager |

## Running Locally

### Docker
```bash
docker compose up -d acct-mgnt-mcp
```

### Direct Python
```bash
cd src/acct-mgnt-mcp
python -m uvicorn main:app --host 0.0.0.0 --port 8083
```

## Testing

### Health Check
```bash
curl http://localhost:8083/health
```

### MCP Endpoints

**List Resources:**
```bash
curl -X POST http://localhost:8083/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "resources/list",
    "id": 1
  }'
```

**List Tools:**
```bash
curl -X POST http://localhost:8083/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/list",
    "id": 2
  }'
```

**Invoke Orchestrator:**
```bash
curl -X POST http://localhost:8083/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "account_management_invoke",
      "arguments": {
        "intent": "Account_PW_Reset",
        "request_context": "I forgot my password",
        "conversation_id": "mcp-test-1"
      }
    },
    "id": 3
  }'
```

Or use the automated test script:
```bash
python tests/test_mcp_server.py
```

## AWS Integration

Authentication is expected to be enforced at the UI/Gateway IAM layer. This service does not perform service-level SigV4 validation or SigV4 request signing.

### Secrets Manager Configuration

The secret must contain:
```json
{
  "AWS_ACCESS_KEY_ID": "AKIA...",
  "AWS_SECRET_ACCESS_KEY": "...",
  "AWS_REGION": "us-east-1"
}
```

## Building ARM64

The Dockerfile is multi-arch safe and the repository build script already forces `linux/arm64`, which is the architecture expected by AgentCore Runtime deployments.

Build only this image locally with Buildx:

```bash
docker buildx build --platform linux/arm64 -t acct-mgnt-mcp:arm64 ./src/acct-mgnt-mcp
```

Or push it with the repository helper script:

```powershell
./build_and_push_to_ecr.ps1 -ImageTag v1
```

## Registering In AgentCore Runtime

`acct-mgnt-mcp` is now exposed as a standard MCP HTTP server at `/mcp`, which matches the AgentCore Runtime `serverProtocol=MCP` registration model.

1. Push the ARM64 image to ECR.
2. Create the AgentCore Runtime with `protocolConfiguration.serverProtocol` set to `MCP`.
3. Create a runtime endpoint for the runtime.
4. Register that runtime endpoint from AgentCore Gateway as an MCP tool server.

Example CLI payload for runtime creation:

```json
{
  "agentRuntimeName": "acct-mgnt-mcp",
  "description": "MCP bridge from AgentCore Gateway to orchestrator-agent",
  "agentRuntimeArtifact": {
    "containerConfiguration": {
      "containerUri": "834458830002.dkr.ecr.us-east-1.amazonaws.com/aie_account_management_svc:acct-mgnt-mcp-v1"
    }
  },
  "roleArn": "arn:aws:iam::834458830002:role/bedrock-agentcore-runtime-role",
  "networkConfiguration": {
    "networkMode": "VPC",
    "networkModeConfig": {
      "securityGroups": ["sg-0123456789abcdef0"],
      "subnets": ["subnet-0123456789abcdef0", "subnet-abcdef01234567890"]
    }
  },
  "protocolConfiguration": {
    "serverProtocol": "MCP"
  },
  "environmentVariables": {
    "AWS_REGION": "us-east-1",
    "SERVICE_ORCHESTRATOR_URL": "http://orchestrator-agent.internal/orchestrate",
    "SERVICE_AGENTCORE_RUNTIME_MCP_PATH": "/mcp"
  }
}
```

Create the runtime:

```bash
aws bedrock-agentcore-control create-agent-runtime --cli-input-json file://runtime-create.json
```

Then create an endpoint:

```json
{
  "agentRuntimeId": "runtime-id-from-create",
  "name": "acct-mgnt-mcp-endpoint"
}
```

```bash
aws bedrock-agentcore-control create-agent-runtime-endpoint --cli-input-json file://runtime-endpoint.json
```

After the endpoint is ready, use its AgentCore Runtime URL in AgentCore Gateway as the MCP integration URL. The gateway should target the runtime base URL and the runtime will expose the MCP transport on `/mcp`.

For direct HTTP callers, include `Accept: application/json, text/event-stream` on MCP requests because the standard streamable HTTP transport negotiates between JSON and SSE responses.

## Connecting to AgentCore Gateway

To register this MCP server as a tool server in AgentCore Gateway:

1. Deploy the container as an AgentCore Runtime and create its runtime endpoint.
2. In AgentCore Gateway, create a new MCP integration that points at the runtime endpoint URL.
3. Configure the integration to use the runtime MCP path `/mcp`.
4. Expose the `orchestrator_invoke` tool to the gateway workflow.
4. Expose the `account_management_invoke` tool to the gateway workflow. The legacy `orchestrator_invoke` alias remains available for compatibility.

## Files

- `main.py` - ASGI entry point for the MCP runtime
- `acct_mgnt_mcp/service.py` - FastMCP server and orchestrator bridge
- `config/application.properties` - Configuration
- `requirements.txt` - Python dependencies
- `Dockerfile` - Container image definition
