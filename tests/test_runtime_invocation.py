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
    "genesys_conversation_id": "7a833b7d-5747-407a-a9c9-5781aea9539f",
    "gecx_session_id": "e97b4552-a8bc-4c0a-a3d1-5029c1a5f217",
    "request_type": "validate_account",
    "member_eid": "70400040700130465465",
    "intent": "Account_Unlock_PW_Reset",
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
        default="validate_account",
        help="request_type to send (validate_account, pw_send_link, end_session).",
    )
    parser.add_argument(
        "--conversation-id",
        default="e97b4552-a8bc-4c0a-a3d1-5029c1a5f217",
        help="gecx_session_id for the test request.",
    )
    parser.add_argument("--raw", action="store_true", help="Print raw response body instead of pretty JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    payload = {**DEFAULT_PAYLOAD}
    payload["request_type"] = args.message
    payload["gecx_session_id"] = args.conversation_id

    print(f"Runtime URL : {args.url}")
    print(f"AWS Region  : {args.region}")
    print(f"AWS Profile : {args.profile or '<default>'}")
    print(f"Request Type: {args.message}")
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
