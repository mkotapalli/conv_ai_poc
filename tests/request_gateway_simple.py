import requests

url = "https://bcbs-dev-convai-gateway-e4ing1lwcu.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

headers = {
     # Remove if using IAM auth
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json"
}

payload = {
    "jsonrpc": "2.0",
    "id": "1",
    "method": "tools/call",
    "params": {
        "name": "bcbs-dev-acct-mgmt-gtwy-mcp-target___invokeAccountManagement",
        "arguments": {
            "request_type": "validate_account",
            "gecx_session_id": "session-12345",
            "genesys_conversation_id": "conversation-12345",
            "member_eid": "123456789",
            "intent": "password_reset"
        }
    }
}

response = requests.post(url, headers=headers, json=payload)
import json
print(response.status_code)
print(json.dumps(response.json(), indent=2))