#!/usr/bin/env python
"""
Diagnostic script to check if the account agent A2A endpoint is properly initialized.
"""

import json
import sys

import requests


def main() -> None:
    print("=" * 80)
    print("Account Agent Diagnostic Check")
    print("=" * 80)
    print()

    agent_base = "http://localhost:8082"

    # Test 1: Check root endpoint
    print("1. Checking root endpoint (/)...")
    try:
        response = requests.get(f"{agent_base}/", timeout=5)
        print(f"   Status: {response.status_code}")
        print(f"   Response: {response.json()}")
        print("   ✓ Root endpoint works\n")
    except Exception as e:
        print(f"   ✗ Error: {e}\n")
        return

    # Test 2: Check health endpoint
    print("2. Checking health endpoint (/health)...")
    try:
        response = requests.get(f"{agent_base}/health", timeout=5)
        print(f"   Status: {response.status_code}")
        health = response.json()
        print(f"   Response: {json.dumps(health, indent=2)}")
        print()

        # Show agent status
        agent_error = health.get("agent_last_error", "")
        if agent_error:
            print(f"   ⚠️  Agent Error: {agent_error}")
            print()
        else:
            print("   ✓ No agent errors\n")
    except Exception as e:
        print(f"   ✗ Error: {e}\n")
        return

    # Test 3: Check A2A endpoints
    print("3. Checking A2A endpoint (/a2a)...")
    print("   This endpoint should support A2A JSON-RPC protocol")
    print()

    # Try OPTIONS to see what methods are allowed
    try:
        response = requests.options(f"{agent_base}/a2a", timeout=5)
        print(f"   OPTIONS response status: {response.status_code}")
        allowed = response.headers.get("Allow", "Not specified")
        print(f"   Allowed methods: {allowed}")
        print()
    except Exception as e:
        print(f"   (OPTIONS request failed: {e})\n")

    # Try GET (should fail if A2A is properly mounted)
    print("   Trying GET /a2a (A2A endpoints typically don't accept GET)...")
    try:
        response = requests.get(f"{agent_base}/a2a", timeout=5)
        print(f"   Status: {response.status_code}")
        if response.status_code == 405:
            print("   ✓ Expected: 405 Method Not Allowed (A2A doesn't support GET)")
        else:
            print(f"   ? Unexpected status: {response.status_code}")
        print()
    except Exception as e:
        print(f"   Error: {e}\n")

    # Check if the problem is that A2A app wasn't mounted
    print("4. Summary:")
    print()
    print("   If agent_last_error is empty and A2A returns 405,  then A2A is properly mounted.")
    print("   If agent_last_error shows an error, the Strands agent failed to initialize.")
    print()
    print("   Common causes:")
    print("   - AWS credentials not configured (check ~/.aws/credentials or env vars)")
    print("   - Bedrock model not available in your region")
    print("   - Secrets Manager secret not accessible")
    print()


if __name__ == "__main__":
    main()
