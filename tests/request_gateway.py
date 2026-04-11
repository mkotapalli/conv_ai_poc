from __future__ import annotations

import argparse
import json

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a test request to the AgentCore POC gateway.")
    parser.add_argument(
        "--url",
        default="http://localhost:8080/chat",
        help="Gateway chat endpoint URL (default: http://localhost:8080/chat)",
    )
    parser.add_argument(
        "--conversation-id",
        default="conv-python-test-1",
        help="Conversation ID to send with the request",
    )
    parser.add_argument(
        "--message",
        default="what can you do for me ?",
        help="User message to send to the gateway",
    )
    parser.add_argument(
        "--token",
        default="local-dev-token",
        help="Bearer token to send in the Authorization header",
    )
    args = parser.parse_args()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.token}",
    }
    payload = {
        "conversation_id": args.conversation_id,
        "message": args.message,
    }

    response = requests.post(args.url, headers=headers, json=payload, timeout=30)
    print(f"status={response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)

    response.raise_for_status()


if __name__ == "__main__":
    main()
