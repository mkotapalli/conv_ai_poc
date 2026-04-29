from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests

try:
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
except ImportError:
    boto3 = None
    SigV4Auth = None
    AWSRequest = None


DEFAULT_DIRECT_MCP_URL = os.getenv("DIRECT_MCP_URL", "")
DEFAULT_TOOL_NAME = "orchestrator_invoke"


def parse_mcp_response(response: requests.Response) -> dict[str, Any]:
    """Parse plain JSON or SSE-wrapped FastMCP responses."""
    content_type = response.headers.get("content-type", "")
    text = response.text.strip()

    if "text/event-stream" in content_type or text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise ValueError(f"No data: line found in SSE response:\n{text}")

    return response.json()


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

    if boto3 is None or SigV4Auth is None or AWSRequest is None:
        raise RuntimeError(
            "SigV4 requested but boto3/botocore are unavailable. Install dependencies first."
        )

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


def rpc_call(
    *,
    url: str,
    method: str,
    rpc_id: int,
    params: dict[str, Any] | None,
    timeout_seconds: int,
    region: str,
    profile: str | None,
    use_sigv4: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": rpc_id}
    if params is not None:
        payload["params"] = params

    body = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {
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

    response = requests.post(url, data=body, headers=signed_headers, timeout=timeout_seconds)
    response.raise_for_status()
    return parse_mcp_response(response)


SAMPLE_CONTRACT_BY_INTENT: dict[str, str] = {
    "Account_Unlock": "918783081",
    "Account_PW_Reset": "3413623345",
}


def build_sample_tool_args(intent: str) -> dict[str, str]:
    contract = SAMPLE_CONTRACT_BY_INTENT.get(intent, "918783081")
    return {
        "intent": intent,
        "conversation_id": "123",
        "request_context": "basic LLM message from direct MCP test",
        "member_contract_number": contract,
        "member_birth_date": "1971-03-11",
        "member_zip": "49503",
        "member_eid": "406070601060300",
        "member_group_number": "00257995",
        "member_group_suffix": "0004",
    }


def infer_sigv4(url: str, force_sigv4: bool) -> bool:
    if force_sigv4:
        return True
    return "bedrock-agentcore" in url


def run(args: argparse.Namespace) -> int:
    if not args.url:
        print("ERROR: Missing --url (or DIRECT_MCP_URL env var).")
        print("Example:")
        print(
            "  python tests\\request_mcp_direct.py --url \"https://.../mcp\" --call-sample"
        )
        return 2

    use_sigv4 = infer_sigv4(args.url, args.sigv4)

    print(f"MCP URL: {args.url}")
    print(f"Use SigV4: {use_sigv4}")
    print(f"Region: {args.region}")
    print(f"Timeout: {args.timeout}s")
    if args.profile:
        print(f"AWS Profile: {args.profile}")

    try:
        print("\n[1/3] tools/list")
        tools = rpc_call(
            url=args.url,
            method="tools/list",
            rpc_id=1,
            params=None,
            timeout_seconds=args.timeout,
            region=args.region,
            profile=args.profile,
            use_sigv4=use_sigv4,
        )
        print(json.dumps(tools, indent=2))

        if not args.skip_resources:
            print("\n[2/3] resources/list")
            resources = rpc_call(
                url=args.url,
                method="resources/list",
                rpc_id=2,
                params=None,
                timeout_seconds=args.timeout,
                region=args.region,
                profile=args.profile,
                use_sigv4=use_sigv4,
            )
            print(json.dumps(resources, indent=2))

        if args.call_sample:
            print(f"\n[3/3] tools/call ({args.tool_name})")
            tool_args = build_sample_tool_args(args.sample_intent)
            result = rpc_call(
                url=args.url,
                method="tools/call",
                rpc_id=3,
                params={"name": args.tool_name, "arguments": tool_args},
                timeout_seconds=args.timeout,
                region=args.region,
                profile=args.profile,
                use_sigv4=use_sigv4,
            )
            print("Request:")
            print(json.dumps(tool_args, indent=2))
            print("Response:")
            print(json.dumps(result, indent=2))

        print("\nPASS: Direct MCP test completed.")
        return 0
    except Exception as exc:
        print("\nFAIL: Direct MCP test failed.")
        print(f"Reason: {exc}")
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Direct MCP test bypassing gateway/Okta by calling MCP endpoint directly."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_DIRECT_MCP_URL,
        help="Direct MCP URL (for example runtime URL ending with /mcp).",
    )
    parser.add_argument("--timeout", type=int, default=60, help="Timeout in seconds.")
    parser.add_argument("--region", default="us-east-1", help="AWS region for SigV4.")
    parser.add_argument("--profile", default="", help="Optional AWS CLI profile for SigV4.")
    parser.add_argument(
        "--sigv4",
        action="store_true",
        help="Force SigV4 signing even if URL does not contain bedrock-agentcore.",
    )
    parser.add_argument(
        "--skip-resources",
        action="store_true",
        help="Skip resources/list.",
    )
    parser.add_argument(
        "--call-sample",
        action="store_true",
        help="Call sample tool request after listing tools/resources.",
    )
    parser.add_argument(
        "--tool-name",
        default=DEFAULT_TOOL_NAME,
        help="Tool name for tools/call (default: orchestrator_invoke).",
    )
    parser.add_argument(
        "--sample-intent",
        default="Account_Unlock",
        choices=["Account_PW_Reset", "Account_Unlock"],
        help="Intent for sample request.",
    )
    args = parser.parse_args()
    args.profile = args.profile.strip() or None
    return args


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())

#python tests\request_mcp_direct.py --url "https://<your-direct-mcp-endpoint>/mcp" --call-sample --sample-intent Account_Unlock
#python tests\request_mcp_direct.py --url "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A834458830002%3Aruntime%2Fbcbs_dev_convai_mcp-Vt2A72DKeq/invocations" --call-sample --sample-intent Account_PW_Reset
#python tests\request_mcp_direct.py --url "https://bedrock-agentcore.us-east-1.amazonaws.com/..." --profile <your-profile> --call-sample