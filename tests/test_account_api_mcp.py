#!/usr/bin/env python3
"""Basic integration checks for account-api-mcp."""

import json

import requests

BASE_URL = "http://localhost:8084"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def parse_mcp_response(response: requests.Response) -> dict:
    content_type = response.headers.get("content-type", "")
    text = response.text.strip()

    if "text/event-stream" in content_type or text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise ValueError(f"No data line found in SSE response: {text}")

    return response.json()


def call_lookup(intent: str, contract_number: str, req_id: int) -> dict:
    response = requests.post(
        f"{BASE_URL}/mcp",
        headers=HEADERS,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "account_api_lookup",
                "arguments": {
                    "intent": intent,
                    "contract_number": contract_number,
                },
            },
            "id": req_id,
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = parse_mcp_response(response)
    text = payload["result"]["content"][0]["text"]
    return json.loads(text)


def main() -> None:
    print("Testing account-api-mcp health...")
    health = requests.get(f"{BASE_URL}/health", timeout=5)
    health.raise_for_status()
    print(json.dumps(health.json(), indent=2))

    print("\nTesting known unlock contract...")
    unlock = call_lookup("Account_Unlock", "918783081", 1)
    print(json.dumps(unlock, indent=2))

    print("\nTesting known reset contract...")
    reset = call_lookup("Account_reset", "3413623345", 2)
    print(json.dumps(reset, indent=2))

    print("\nTesting unknown contract...")
    unknown = call_lookup("Account_Unlock", "000000000", 3)
    print(json.dumps(unknown, indent=2))


if __name__ == "__main__":
    main()
