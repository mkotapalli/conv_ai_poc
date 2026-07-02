"""Run the full account recovery flow end to end against the account-management runtime."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

try:
    import boto3
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency. Install boto3 and requests: pip install boto3 requests"
    ) from exc


DEFAULT_RUNTIME_URL = (
    "https://bedrock-agentcore.us-east-1.amazonaws.com"
    "/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A834458830002%3Aruntime%2F"
    "bcbs_dev_convai_acct_mgnt-8xi3SACIES/invocations?qualifier=bcbs_dev_acct_mgmt_endpoint"
)


@dataclass(frozen=True)
class FlowIds:
    genesys_conversation_id: str
    gecx_session_id: str
    member_eid: str
    intent: str


def infer_sigv4(url: str, force_sigv4: bool) -> bool:
    if force_sigv4:
        return True
    return "bedrock-agentcore" in url


def sign_headers_if_needed(
    *,
    url: str,
    body: bytes,
    headers: dict[str, str],
    region: str,
    profile: str | None,
    use_sigv4: bool,
) -> dict[str, str]:
    if not use_sigv4:
        return headers

    if profile:
        session = boto3.Session(profile_name=profile, region_name=region)
    else:
        session = boto3.Session(region_name=region)

    credentials_provider = session.get_credentials()
    if credentials_provider is None:
        raise RuntimeError("No AWS credentials found for SigV4 signing.")

    credentials = credentials_provider.get_frozen_credentials()
    request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)
    return dict(request.headers)


def post_json(
    *,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    region: str,
    profile: str | None,
    use_sigv4: bool,
) -> requests.Response:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    signed_headers = sign_headers_if_needed(
        url=url,
        body=body,
        headers=headers,
        region=region,
        profile=profile,
        use_sigv4=use_sigv4,
    )

    return requests.post(url, data=body, headers=signed_headers, timeout=timeout_seconds)


def print_step(title: str, request_payload: dict[str, Any], response: requests.Response) -> dict[str, Any]:
    print(f"\n=== {title} ===")
    print("Request:")
    print(json.dumps(request_payload, indent=2))
    print(f"HTTP Status: {response.status_code}")
    print("Response:")
    try:
        parsed = response.json()
        print(json.dumps(parsed, indent=2))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        print(response.text)
        return {}


def status_is_success(payload: dict[str, Any]) -> bool:
    return str(payload.get("request_status") or "").strip().lower() == "success"


def run(args: argparse.Namespace) -> int:
    runtime_url = (args.url or os.getenv("E2E_RUNTIME_URL", "")).strip()
    if not runtime_url:
        print("ERROR: Missing --url (or E2E_RUNTIME_URL env var).")
        return 2

    use_sigv4 = infer_sigv4(runtime_url, args.sigv4)
    flow_ids = FlowIds(
        genesys_conversation_id=args.genesys_conversation_id,
        gecx_session_id=args.gecx_session_id,
        member_eid=args.member_eid,
        intent=args.intent,
    )

    print(f"Runtime URL : {runtime_url}")
    print(f"Use SigV4   : {use_sigv4}")
    print(f"Region      : {args.region}")
    print(f"Profile     : {args.profile or '<default>'}")
    print(f"Delivery    : {args.delivery_type}")

    validate_payload = {
        "genesys_conversation_id": flow_ids.genesys_conversation_id,
        "gecx_session_id": flow_ids.gecx_session_id,
        "request_type": "validate_account",
        "member_eid": flow_ids.member_eid,
        "intent": flow_ids.intent,
    }
    validate_response = post_json(
        url=runtime_url,
        payload=validate_payload,
        timeout_seconds=args.timeout,
        region=args.region,
        profile=args.profile,
        use_sigv4=use_sigv4,
    )
    validate_result = print_step("1) validate_account", validate_payload, validate_response)
    if validate_response.status_code != 200:
        return 1

    aie_session_id = str(validate_result.get("aie_session_id") or "").strip()
    if not aie_session_id:
        print("\nFAIL: validate_account did not return aie_session_id.")
        return 1

    if not status_is_success(validate_result):
        print(
            "\nFAIL: validate_account returned request_status != Success. "
            "Skipping pw_send_link because the session may not be valid."
        )
        print(f"request_status: {validate_result.get('request_status', '<missing>')}")
        print(f"request_status_msg: {validate_result.get('request_status_msg', '<missing>')}")
        return 1

    link_payload = {
        "genesys_conversation_id": flow_ids.genesys_conversation_id,
        "gecx_session_id": flow_ids.gecx_session_id,
        "aie_session_id": aie_session_id,
        "request_type": "pw_send_link",
        "delivery_type": args.delivery_type,
        "member_eid": flow_ids.member_eid,
        "intent": flow_ids.intent,
    }
    link_response = post_json(
        url=runtime_url,
        payload=link_payload,
        timeout_seconds=args.timeout,
        region=args.region,
        profile=args.profile,
        use_sigv4=use_sigv4,
    )
    link_result = print_step("2) pw_send_link", link_payload, link_response)
    if link_response.status_code != 200:
        return 1

    link_status_msg = str(link_result.get("request_status_msg") or "").strip()
    if link_status_msg == "No active session. Call validate_account first.":
        print(
            "\nFAIL: Response came from the legacy orchestrator path (old message text detected).\n"
            "Update the runtime URL to acct-mgmt-agent and confirm MCP uses SERVICE_ACCOUNT_AGENT_INVOCATIONS_URL."
        )
        return 1

    end_payload = {
        "genesys_conversation_id": flow_ids.genesys_conversation_id,
        "gecx_session_id": flow_ids.gecx_session_id,
        "aie_session_id": aie_session_id,
        "request_type": "end_session",
    }
    end_response = post_json(
        url=runtime_url,
        payload=end_payload,
        timeout_seconds=args.timeout,
        region=args.region,
        profile=args.profile,
        use_sigv4=use_sigv4,
    )
    end_result = print_step("3) end_session", end_payload, end_response)
    if end_response.status_code != 200:
        return 1

    print("\nPASS: End-to-end flow completed successfully.")
    print(f"aie_session_id: {aie_session_id}")
    print(f"validate_account status: {validate_result.get('request_status', '<missing>')}")
    print(f"pw_send_link status: {link_result.get('request_status', '<missing>')}")
    print(f"end_session status: {end_result.get('request_status', '<missing>')}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the account recovery flow end to end.")
    parser.add_argument(
        "--url",
        default=DEFAULT_RUNTIME_URL,
        help="Account-management runtime URL. If omitted, falls back to E2E_RUNTIME_URL.",
    )
    parser.add_argument("--region", default="us-east-1", help="AWS region.")
    parser.add_argument("--profile", default=None, help="AWS CLI profile name (optional).")
    parser.add_argument("--timeout", type=int, default=120, help="Request timeout in seconds.")
    parser.add_argument("--sigv4", action="store_true", help="Force SigV4 signing.")
    parser.add_argument(
        "--delivery-type",
        default="sms",
        choices=["sms", "email"],
        help="Delivery channel used for pw_send_link.",
    )
    parser.add_argument(
        "--genesys-conversation-id",
        default="7a833b7d-5747-407a-a9c9-5781aea9539f",
        help="Genesys conversation ID.",
    )
    parser.add_argument(
        "--gecx-session-id",
        default="e97b4552-a8bc-4c0a-a3d1-5029c1a5f217",
        help="GECX session ID.",
    )
    parser.add_argument(
        "--member-eid",
        default="70400040700130465465",
        help="Member EID used for validation.",
    )
    parser.add_argument(
        "--intent",
        default="Account_Unlock_PW_Reset",
        help="Intent used throughout the flow.",
    )
    args = parser.parse_args()
    args.profile = args.profile.strip() or None if args.profile else None
    return args


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())