"""Test AgentCore runtime invocation endpoint directly via AWS SigV4-signed HTTP POST."""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import boto3
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session as BotocoreSession
except ImportError as exc:
    raise SystemExit(
        "Missing dependency. Install boto3 and requests: pip install boto3 requests"
    ) from exc

DEFAULT_RUNTIME_URL = (
    "https://bedrock-agentcore.us-east-1.amazonaws.com"
    "/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A834458830002%3Aruntime%2F"
    "bcbs_dev_convai_orchestrator-GEkw1YGeCY/invocations?qualifier=bcbs_dev_orchestrator_endpoint"
)

DEFAULT_PAYLOAD = {
    "intent": None,
    "conversation_id": "test-conv-001",
    "member_context": {
        "member_contract_number": "918783081",
        "member_birth_date": "1971-03-11",
        "member_zip": "49503",
        "member_eid": "406070601060300",
        "member_group_number": "00257995",
        "member_group_suffix": "0004",
    },
    "request_context": "smoke test from test_runtime_invocation.py",
    "user_context": {"subject": "smoke-test-user"},
}


def make_signed_request(url: str, payload: dict, region: str, profile: str | None) -> requests.Response:
    """Sign the request with SigV4 and send it."""
    session = boto3.Session(profile_name=profile, region_name=region)
    credentials = session.get_credentials().get_frozen_credentials()

    body = json.dumps(payload).encode("utf-8")

    aws_request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    botocore_session = BotocoreSession()
    botocore_session.set_credentials(
        credentials.access_key,
        credentials.secret_key,
        credentials.token,
    )

    SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(aws_request)

    prepped = requests.Request(
        method=aws_request.method,
        url=aws_request.url,
        headers=dict(aws_request.headers),
        data=body,
    ).prepare()

    with requests.Session() as http_session:
        return http_session.send(prepped, timeout=120)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test AgentCore runtime invocation endpoint.")
    parser.add_argument("--url", default=DEFAULT_RUNTIME_URL, help="Full runtime invocation URL.")
    parser.add_argument("--region", default="us-east-1", help="AWS region.")
    parser.add_argument("--profile", default=None, help="AWS CLI profile name (optional).")
    parser.add_argument(
        "--message",
        default="Please unlock my account, I am locked out.",
        help="Test message to send as request_context.",
    )
    parser.add_argument(
        "--conversation-id",
        default="test-conv-001",
        help="Conversation ID for the test request.",
    )
    parser.add_argument("--raw", action="store_true", help="Print raw response body instead of pretty JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    payload = {**DEFAULT_PAYLOAD}
    payload["request_context"] = args.message
    payload["conversation_id"] = args.conversation_id

    print(f"Runtime URL : {args.url}")
    print(f"AWS Region  : {args.region}")
    print(f"AWS Profile : {args.profile or '<default>'}")
    print(f"Message     : {args.message}")
    print()

    print("Sending signed POST request...")
    try:
        response = make_signed_request(
            url=args.url,
            payload=payload,
            region=args.region,
            profile=args.profile,
        )
    except Exception as exc:
        print(f"FAIL: Request error - {exc}")
        return 1

    print(f"HTTP Status : {response.status_code}")
    print(f"Headers     : {dict(response.headers)}")
    print()

    if args.raw:
        print("Response Body:")
        print(response.text)
    else:
        print("Response Body:")
        try:
            print(json.dumps(response.json(), indent=2))
        except json.JSONDecodeError:
            print(response.text)

    if response.status_code == 200:
        print("\nPASS: Runtime invocation returned HTTP 200.")
        return 0
    elif response.status_code == 424:
        print(
            "\nFAIL: HTTP 424 (Failed Dependency) — AgentCore reached your container but the "
            "container returned an error (typically 404 or 405).\n"
            "Likely causes:\n"
            "  1. AgentCore invokes the container at a path your app does not handle\n"
            "     (e.g. POST /invocations). Add that route or check the runtime path config.\n"
            "  2. The container port in the AgentCore console does not match what the container\n"
            "     actually listens on (Dockerfile EXPOSE / uvicorn --port).\n"
            "  3. The container crashed on startup — check CloudWatch logs for the runtime.\n"
            "  Tip: curl <RUNTIME_ENDPOINT_URL>/health directly to verify the container is running."
        )
        return 1
    elif response.status_code == 401 or response.status_code == 403:
        print(
            f"\nFAIL: HTTP {response.status_code} — AWS authentication/authorization failed.\n"
            "  Check your AWS credentials and that your IAM principal has\n"
            "  'bedrock-agentcore:InvokeRuntime' permission on the runtime ARN."
        )
        return 1
    else:
        print(f"\nFAIL: Unexpected HTTP status {response.status_code}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
