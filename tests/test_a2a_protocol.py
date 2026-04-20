#!/usr/bin/env python
"""
Test A2A protocol communication with the account agent.
Mimics what the orchestrator's A2A client does.
"""

import json
import sys

import requests


def main() -> None:
    print("=" * 80)
    print("Testing A2A Protocol Communication")
    print("=" * 80)
    print()

    a2a_url = "http://localhost:8082/a2a"

    # A2A protocol typically starts with an agent_card request
    # This is what the A2A client does when establishing a connection

    print("1. Attempting A2A agent card request (get protocol metadata)...")
    print()

    # A2A agent card request (JSON-RPC 2.0 format)
    card_request = {
        "jsonrpc": "2.0",
        "method": "agent_card",
        "params": {},
        "id": 1,
    }

    print("Request payload:")
    print(json.dumps(card_request, indent=2))
    print()

    try:
        response = requests.post(
            a2a_url,
            json=card_request,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )

        print(f"Response Status: {response.status_code}")
        print()

        if response.status_code == 200:
            print("Response Body:")
            resp_json = response.json()
            print(json.dumps(resp_json, indent=2))
            print()
            print("✓ A2A server responded successfully!")
        else:
            print(f"✗ Unexpected status: {response.status_code}")
            print("Response:")
            print(response.text)

    except Exception as e:
        print(f"✗ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
