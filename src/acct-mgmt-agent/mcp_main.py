from __future__ import annotations

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from acct_mgmt_agent.service import AccountManagementService

SERVICE = AccountManagementService()

MCP_SERVER = FastMCP(
    name=SERVICE.settings.get("app.name", "acct-mgmt-agent"),
    instructions=(
        "Expose account-management MCP tools. Use account_management_invoke "
        "for validate_account, pw_send_link, and end_session flows."
    ),
    host=SERVICE.settings.get("mcp.server.host", "0.0.0.0"),
    port=int(SERVICE.settings.get("mcp.server.port", "8000")),
    stateless_http=True,
)


@MCP_SERVER.resource(
    "account://password-reset",
    name="Password Reset",
    description="Guidance for password reset requests handled by acct-mgmt-agent.",
    mime_type="application/json",
)
def password_reset_resource() -> str:
    return json.dumps(
        {
            "action": "password_reset",
            "target": "acct-mgmt-agent",
            "tool": "account_management_invoke",
        }
    )


@MCP_SERVER.resource(
    "account://account-unlock",
    name="Account Unlock",
    description="Guidance for account unlock requests handled by acct-mgmt-agent.",
    mime_type="application/json",
)
def account_unlock_resource() -> str:
    return json.dumps(
        {
            "action": "account_unlock",
            "target": "acct-mgmt-agent",
            "tool": "account_management_invoke",
        }
    )


@MCP_SERVER.tool(
    name="account_management_invoke",
    description=(
        "Invoke the account-management agent directly for account validation, "
        "password-link delivery, and end-session flows."
    ),
)
def account_management_invoke(
    genesys_conversation_id: Optional[str] = None,
    gecx_session_id: Optional[str] = None,
    aie_session_id: Optional[str] = None,
    request_type: Optional[str] = None,
    member_eid: Optional[str] = None,
    delivery_type: Optional[str] = None,
    intent: Optional[str] = None,
) -> str:
    payload = {
        "genesys_conversation_id": (genesys_conversation_id or "").strip(),
        "gecx_session_id": (gecx_session_id or "").strip(),
        "aie_session_id": (aie_session_id or "").strip(),
        "request_type": (request_type or "").strip(),
        "member_eid": (member_eid or "").strip(),
        "delivery_type": (delivery_type or "").strip().lower(),
        "intent": (intent or "").strip(),
    }
    result = SERVICE.handle_request(payload)
    return json.dumps(result)


@MCP_SERVER.custom_route("/", methods=["GET"], include_in_schema=False)
async def root(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "service": SERVICE.settings.get("app.name", "acct-mgmt-agent"),
            "message": "Use POST /mcp for MCP and GET /health for service health.",
        }
    )


@MCP_SERVER.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    health_payload = SERVICE.health()
    health_payload["mcp_port"] = SERVICE.settings.get("mcp.server.port", "8000")
    return JSONResponse(health_payload)


app = MCP_SERVER.streamable_http_app()
