#!/usr/bin/env python
"""
Test script for full MCP → Orchestrator → Account Management Agent chain.

This script sends a request through the MCP server to the orchestrator,
which then delegates to the account management agent via A2A handoff.

Usage:
    python test_chain.py [--message "your message"] [--conversation-id "conv-id"]
"""

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the full MCP → Orchestrator → Account Management Agent chain"
    )
    parser.add_argument(
        "--mcp-url",
        default="http://localhost:8083/mcp",
        help="MCP server endpoint (default: http://localhost:8083/mcp)",
    )
    parser.add_argument(
        "--message",
        default="I forgot my password and need to reset it",
        help="User message to send through the chain",
    )
    parser.add_argument(
        "--conversation-id",
        default="local-test-1",
        help="Conversation ID for tracking",
    )
    parser.add_argument(
        "--user-id",
        default="test-user@company.com",
        help="User ID in context",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Request timeout in seconds",
    )
    args = parser.parse_args()

    # MCP request using JSON-RPC 2.0 format for tools/call
    mcp_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "orchestrator_invoke",
            "arguments": {
                "message": args.message,
                "conversation_id": args.conversation_id,
                "user_context": {
                    "user_id": args.user_id,
                    "subject": "local-test",
                },
            },
        },
        "id": 1,
    }

    print("=" * 80)
    print("Full Chain Test: MCP → Orchestrator → Account Management Agent")
    print("=" * 80)
    print()
    print(f"MCP URL:         {args.mcp_url}")
    print(f"Message:         {args.message}")
    print(f"Conversation ID: {args.conversation_id}")
    print(f"User ID:         {args.user_id}")
    print(f"Timeout:         {args.timeout}s")
    print()
    print("-" * 80)
    print("Request payload:")
    print("-" * 80)
    print(json.dumps(mcp_payload, indent=2))
    print()
    print("-" * 80)
    print("Sending request...")
    print("-" * 80)

    try:
        response = requests.post(
            args.mcp_url,
            json=mcp_payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=args.timeout,
            stream=True,
        )

        print(f"Status Code: {response.status_code}")
        print()

        if response.status_code == 200:
            print("-" * 80)
            print("Response (Success):")
            print("-" * 80)
            
            # Handle Server-Sent Events (SSE) from MCP streamable HTTP
            response_texts = []
            for line in response.iter_lines():
                if line:
                    line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                    if line_str.startswith("data: "):
                        data_part = line_str[6:]  # Remove "data: " prefix
                        response_texts.append(data_part)
                        print(data_part)
            
            if not response_texts:
                # Try to parse as regular JSON if no SSE data found
                response.raw.seek(0)
                response_json = response.json()
                print(json.dumps(response_json, indent=2))
            else:
                # Parse the collected SSE data
                full_response = "".join(response_texts)
                try:
                    response_json = json.loads(full_response)
                except json.JSONDecodeError:
                    # Might be multiple JSON objects, try to extract the result
                    lines = full_response.strip().split("\n")
                    for json_line in lines:
                        if json_line.strip():
                            try:
                                response_json = json.loads(json_line)
                                break
                            except json.JSONDecodeError:
                                continue
            
            print()
            print(json.dumps(response_json, indent=2))
            print()

            # Extract key info from chain
            if isinstance(response_json, dict):
                # MCP returns JSON-RPC format with result
                if "result" in response_json:
                    result = response_json.get("result", {})
                    is_error = result.get("isError", False)
                    
                    # Get structured content or parse text content
                    structured = result.get("structuredContent", {})
                    if not structured and "content" in result:
                        content_list = result.get("content", [])
                        if content_list and isinstance(content_list[0], dict):
                            text_content = content_list[0].get("text", "")
                            try:
                                structured = json.loads(text_content)
                            except json.JSONDecodeError:
                                pass
                    
                    if structured:
                        status = structured.get("status", "unknown")
                        print("-" * 80)
                        print("Chain Summary:")
                        print("-" * 80)
                        print(f"MCP Status:          {status}")
                        print(f"Conversation ID:     {structured.get('conversation_id', 'N/A')}")
                        print(f"Delivery Mode:       {structured.get('delivery_mode', 'N/A')}")

                        # Check orchestrator response
                        orch_response = structured.get("orchestrator_response", {})
                        if isinstance(orch_response, dict):
                            print()
                            print(f"Orchestrator Status: {orch_response.get('status', 'N/A')}")
                            print(f"Intent Detected:     {orch_response.get('intent', 'N/A')}")
                            print(f"Intent Confidence:   {orch_response.get('confidence', 'N/A')}")
                            print(f"Routed To:           {orch_response.get('routed_to', 'N/A')}")
                            print(f"Orchestrator Mode:   {orch_response.get('delivery_mode', 'N/A')}")

                            # Check for account agent response
                            answer = orch_response.get("answer", "")
                            if answer:
                                print()
                                print("Orchestrator Answer:")
                                print(f"  {answer[:300]}")
                                if len(answer) > 300:
                                    print("  ...")

                print()
                if is_error:
                    print("✗ Chain encountered an error (see details above)")
                    sys.exit(1)
                else:
                    print("✓ Full chain executed successfully!")
                return

        else:
            print("-" * 80)
            print("Response (Error):")
            print("-" * 80)
            print(f"Status: {response.status_code}")
            try:
                error_json = response.json()
                print(json.dumps(error_json, indent=2))
            except ValueError:
                print(response.text)
            print()
            print("✗ Chain test failed!")
            sys.exit(1)

    except requests.exceptions.ConnectionError as e:
        print()
        print("✗ Connection Error:")
        print(f"  Could not connect to MCP at {args.mcp_url}")
        print(f"  Make sure all three services are running:")
        print("    - Orchestrator: http://localhost:8081")
        print("    - Account Agent: http://localhost:8082")
        print("    - MCP Server: http://localhost:8083")
        print()
        print(f"  Error: {e}")
        sys.exit(1)

    except requests.exceptions.Timeout as e:
        print()
        print("✗ Request Timeout:")
        print(f"  The request took longer than {args.timeout} seconds to complete.")
        print("  Check if all services are responsive and not blocked.")
        print()
        print(f"  Error: {e}")
        sys.exit(1)

    except requests.exceptions.RequestException as e:
        print()
        print("✗ Request Error:")
        print(f"  {e}")
        sys.exit(1)

    except json.JSONDecodeError as e:
        print()
        print("✗ JSON Decode Error:")
        print("  Response was not valid JSON.")
        print(f"  Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
