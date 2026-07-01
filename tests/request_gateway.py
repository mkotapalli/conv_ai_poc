from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'mcp'. Install project requirements first (pip install -r src/acct-mgmt-agent/requirements.txt)."
    ) from exc


DEFAULT_GATEWAY_URL = (
    "https://bcbs-dev-convai-gateway-e4ing1lwcu.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
)


def build_sample_tool_args(request_type: str) -> dict[str, str]:
    return {
        "genesys_conversation_id": "7a833b7d-5747-407a-a9c9-5781aea9539f",
        "gecx_session_id": "e97b4552-a8bc-4c0a-a3d1-5029c1a5f217",
        "request_type": request_type,
        "member_eid": "70400040700130465465",
        "delivery_type": "sms",
        "intent": "Account_Unlock_PW_Reset",
    }


def build_headers(token_env: str, api_key_env: str) -> dict[str, str]:
    headers: dict[str, str] = {}

    bearer_token = os.getenv(token_env, "").strip()
    api_key = os.getenv(api_key_env, "").strip()

    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if api_key:
        headers["x-api-key"] = api_key

    return headers


def _safe_get(result: Any, key: str, default: Any) -> Any:
    if hasattr(result, key):
        return getattr(result, key)
    if isinstance(result, dict):
        return result.get(key, default)
    return default


async def run_test(
    url: str,
    timeout_seconds: int,
    token_env: str,
    api_key_env: str,
    skip_resources: bool,
    call_sample: bool,
    sample_request_type: str,
) -> int:
    headers = build_headers(token_env=token_env, api_key_env=api_key_env)

    print(f"Gateway URL: {url}")
    print(f"Headers: {list(headers.keys()) or ['<none>']}")
    print(f"Timeout: {timeout_seconds}s")

    try:
        async with streamablehttp_client(
            url=url,
            headers=headers,
            timeout=timeout_seconds,
            terminate_on_close=False,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                print("\n[1/3] initialize()")
                initialize_result = await session.initialize()
                print("  OK")
                print(
                    "  Server:",
                    _safe_get(_safe_get(initialize_result, "serverInfo", {}), "name", "<unknown>"),
                )

                print("\n[2/3] list_tools()")
                tools_result = await session.list_tools()
                tools = _safe_get(tools_result, "tools", [])
                print(f"  OK - tools: {len(tools)}")
                for tool in tools:
                    tool_name = _safe_get(tool, "name", "<unnamed>")
                    print(f"    - {tool_name}")

                if not skip_resources:
                    print("\n[3/3] list_resources()")
                    resources_result = await session.list_resources()
                    resources = _safe_get(resources_result, "resources", [])
                    print(f"  OK - resources: {len(resources)}")
                    for resource in resources:
                        uri = _safe_get(resource, "uri", "<no-uri>")
                        print(f"    - {uri}")

                if call_sample:
                    print("\n[4/4] call_tool('bcbs-dev-acct-mgmt-gtwy-mcp-target___orchestrator_invoke') sample request")
                    tool_args = build_sample_tool_args(sample_request_type)
                    call_result = await session.call_tool("bcbs-dev-acct-mgmt-gtwy-mcp-target___orchestrator_invoke", arguments=tool_args)
                    content = _safe_get(call_result, "content", [])
                    print("  Request:")
                    print(json.dumps(tool_args, indent=2))
                    print("  Response content:")
                    if content:
                        for item in content:
                            if hasattr(item, "text") and getattr(item, "text"):
                                print(getattr(item, "text"))
                            else:
                                print(item)
                    else:
                        print(call_result)

        print("\nPASS: Gateway MCP checks completed successfully.")
        return 0
    except Exception as exc:  # pragma: no cover
        print("\nFAIL: Gateway test failed.")
        print(f"Reason: {exc}")
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test AgentCore Gateway MCP endpoint.")
    parser.add_argument("--url", default=DEFAULT_GATEWAY_URL, help="Gateway MCP URL.")
    parser.add_argument("--timeout", type=int, default=60, help="Request timeout in seconds.")
    parser.add_argument(
        "--token-env",
        default="GATEWAY_BEARER_TOKEN",
        help="Environment variable name for bearer token.",
    )
    parser.add_argument(
        "--api-key-env",
        default="GATEWAY_API_KEY",
        help="Environment variable name for x-api-key.",
    )
    parser.add_argument(
        "--skip-resources",
        action="store_true",
        help="Skip list_resources() check.",
    )
    parser.add_argument(
        "--call-sample",
        action="store_true",
        help="Call orchestrator_invoke with a sample payload after checks.",
    )
    parser.add_argument(
        "--sample-request-type",
        default="validate_account",
        choices=["validate_account", "pw_send_link", "end_session"],
        help="request_type value used for --call-sample.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(
        run_test(
            url=args.url,
            timeout_seconds=args.timeout,
            token_env=args.token_env,
            api_key_env=args.api_key_env,
            skip_resources=args.skip_resources,
            call_sample=args.call_sample,
            sample_request_type=args.sample_request_type,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
