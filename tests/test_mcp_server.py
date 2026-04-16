#!/usr/bin/env python3
"""Test script for MCP server integration."""

import json
import requests

BASE_URL = "http://localhost:8083"
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def parse_mcp_response(response: requests.Response) -> dict:
    """Parse a FastMCP response which may be a plain JSON body or SSE-wrapped JSON.

    FastMCP's streamable HTTP transport returns:
        event: message
        data: {"jsonrpc":...}
    Plain callers that request application/json receive the JSON directly.
    This helper handles both formats.
    """
    content_type = response.headers.get("content-type", "")
    text = response.text.strip()

    if "text/event-stream" in content_type or text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise ValueError(f"No data: line found in SSE response:\n{text}")

    return response.json()

def test_health():
    """Test health endpoint."""
    print("Testing health endpoint...")
    response = requests.get(f"{BASE_URL}/health")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}\n")

def test_list_resources():
    """Test listing MCP resources."""
    print("Testing resources/list...")
    response = requests.post(
        f"{BASE_URL}/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "method": "resources/list",
            "id": 1
        }
    )
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(parse_mcp_response(response), indent=2)}\n")

def test_list_tools():
    """Test listing MCP tools."""
    print("Testing tools/list...")
    response = requests.post(
        f"{BASE_URL}/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 2
        }
    )
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(parse_mcp_response(response), indent=2)}\n")

def test_invoke_orchestrator():
    """Test invoking orchestrator through MCP."""
    print("Testing tools/call (invoke orchestrator)...")
    response = requests.post(
        f"{BASE_URL}/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "orchestrator_invoke",
                "arguments": {
                    "message": "I forgot my password",
                    "conversation_id": "mcp-test-user-1",
                    "user_context": {
                        "user_id": "mcp-test-user"
                    }
                }
            },
            "id": 3
        }
    )
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(parse_mcp_response(response), indent=2)}\n")

def test_read_resource():
    """Test reading a specific MCP resource."""
    print("Testing resources/read...")
    response = requests.post(
        f"{BASE_URL}/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "method": "resources/read",
            "params": {
                "uri": "account://password-reset"
            },
            "id": 4
        }
    )
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(parse_mcp_response(response), indent=2)}\n")

if __name__ == "__main__":
    print("=" * 60)
    print("MCP Server Integration Tests")
    print("=" * 60 + "\n")

    try:
        test_health()
        test_list_resources()
        test_list_tools()
        test_read_resource()
        test_invoke_orchestrator()
    except requests.exceptions.ConnectionError:
        print("ERROR: Could not connect to MCP server at", BASE_URL)
        print("Make sure the server is running on port 8083")
    except Exception as e:
        print(f"ERROR: {e}")
