from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

SERVICE_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("CONFIG_FILE", SERVICE_ROOT / "config" / "application.properties"))
LOGGER = logging.getLogger(__name__)


def load_properties(path: Path) -> dict[str, str]:
    properties: dict[str, str] = {}
    if not path.exists():
        return properties

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key.strip()] = value.strip()
    return properties


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.properties = load_properties(path)

    @staticmethod
    def _to_env_key(key: str) -> str:
        return key.upper().replace(".", "_").replace("-", "_")

    def get(self, key: str, default: str = "") -> str:
        env_key = self._to_env_key(key)
        return os.getenv(env_key, self.properties.get(key, default))

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default


class OrchestratorBridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.orchestrator_url = self.settings.get("service.orchestrator.url")

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": self.settings.get("app.name", "acct-mgnt-mcp"),
            "mcp_path": "/mcp",
            "health_path": "/health",
            "orchestrator_url": self.orchestrator_url,
            "runtime_protocol": self.settings.get("service.agentcore.runtime.protocol", "MCP"),
        }

    def invoke(
        self,
        *,
        genesys_conversation_id: str | None,
        gecx_session_id: str | None,
        aie_session_id: str | None,
        request_type: str,
        member_eid: str | None,
        delivery_type: str | None,
        intent: str | None,
    ) -> dict[str, Any]:
        if not self.orchestrator_url:
            raise RuntimeError("Orchestrator URL is missing from configuration.")

        normalized_request_type = request_type.strip()
        if not normalized_request_type:
            raise ValueError("A non-empty request_type is required.")

        payload = {
            "genesys_conversation_id": (genesys_conversation_id or "").strip(),
            "gecx_session_id": (gecx_session_id or "").strip(),
            "aie_session_id": (aie_session_id or "").strip(),
            "request_type": normalized_request_type,
            "member_eid": (member_eid or "").strip(),
            "delivery_type": (delivery_type or "").strip().lower(),
            "intent": (intent or "").strip(),
        }
        headers = {"content-type": "application/json"}
        timeout_seconds = self.settings.get_int("http.timeout.seconds", 30)
        body = json.dumps(payload).encode("utf-8")

        if "bedrock-agentcore" in self.orchestrator_url:
            region = self.settings.get("aws.region", os.getenv("AWS_REGION", "us-east-1"))
            session = boto3.Session(region_name=region)
            credentials = session.get_credentials().get_frozen_credentials()
            aws_request = AWSRequest(
                method="POST",
                url=self.orchestrator_url,
                data=body,
                headers=headers,
            )
            SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(aws_request)
            prepped = requests.Request(
                method="POST",
                url=self.orchestrator_url,
                headers=dict(aws_request.headers),
                data=body,
            ).prepare()
            with requests.Session() as http_session:
                response = http_session.send(prepped, timeout=timeout_seconds)
        else:
            response = requests.post(
                self.orchestrator_url,
                data=body,
                headers=headers,
                timeout=timeout_seconds,
            )
        response.raise_for_status()
        result = response.json()
        return result


SETTINGS = Settings(CONFIG_PATH)
BRIDGE = OrchestratorBridge(SETTINGS)

# AgentCore Runtime requires:
#   - host 0.0.0.0, port 8000  (hard-coded by the platform)
#   - streamable_http_path /mcp (platform default)
#   - stateless_http=True       (recommended and required for horizontal scaling)
MCP_SERVER = FastMCP(
    name=SETTINGS.get("app.name", "acct-mgnt-mcp"),
    instructions=(
        "Expose account-management MCP tools for AgentCore Gateway. Use the orchestrator_invoke tool "
        "to route password-reset, unlock, and general account access requests to orchestrator-agent."
    ),
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
)


# ── Resources ─────────────────────────────────────────────────────────────────
# Resources return str — MCP protocol requires text content, not raw dicts.

@MCP_SERVER.resource(
    "account://password-reset",
    name="Password Reset",
    description="Guidance for password reset requests that should be routed through the orchestrator-agent.",
    mime_type="application/json",
)
def password_reset_resource() -> str:
    return json.dumps({
        "action": "password_reset",
        "description": "Handles user password reset requests through the orchestrator-agent.",
        "target": "orchestrator-agent",
        "tool": "orchestrator_invoke",
    })


@MCP_SERVER.resource(
    "account://account-unlock",
    name="Account Unlock",
    description="Guidance for account unlock requests that should be routed through the orchestrator-agent.",
    mime_type="application/json",
)
def account_unlock_resource() -> str:
    return json.dumps({
        "action": "account_unlock",
        "description": "Handles account unlock requests through the orchestrator-agent.",
        "target": "orchestrator-agent",
        "tool": "orchestrator_invoke",
    })


@MCP_SERVER.resource(
    "account://runtime-registration",
    name="AgentCore Runtime Registration",
    description="Runtime metadata used when registering this MCP service in AgentCore Runtime and Gateway.",
    mime_type="application/json",
)
def runtime_registration_resource() -> str:
    return json.dumps({
        "server_protocol": SETTINGS.get("service.agentcore.runtime.protocol", "MCP"),
        "mcp_path": "/mcp",
        "health_path": "/health",
        "container_port": 8000,
    })


# ── Tool ──────────────────────────────────────────────────────────────────────
# IMPORTANT: AgentCore Gateway rejects tool schemas containing JSON Schema
# keywords like $ref or $defs. Using dict[str, Any] or nested Pydantic models
# in the tool signature causes FastMCP to emit these keywords, which makes
# AgentCore silently drop the tool from tools/list entirely.
#
# Fix: flatten all parameters to simple scalar/Optional[str] types only.
# user_context is kept minimal and derived server-side.
# Return type is str (JSON) instead of dict for the same reason.

@MCP_SERVER.tool(
    name="orchestrator_invoke",
    description=(
        "Forward a user message from AgentCore Gateway to orchestrator-agent "
        "and return the orchestration result. "
        "Use this tool for password reset, account unlock, and general account access requests."
    ),
)
def orchestrator_invoke(
    genesys_conversation_id: Optional[str] = None,
    gecx_session_id: Optional[str] = None,
    aie_session_id: Optional[str] = None,
    request_type: Optional[str] = None,
    member_eid: Optional[str] = None,
    delivery_type: Optional[str] = None,
    intent: Optional[str] = None,
) -> str:
    result = BRIDGE.invoke(
        genesys_conversation_id=(genesys_conversation_id or "").strip(),
        gecx_session_id=(gecx_session_id or "").strip(),
        aie_session_id=(aie_session_id or "").strip(),
        request_type=(request_type or "").strip(),
        member_eid=(member_eid or "").strip(),
        delivery_type=(delivery_type or "").strip().lower(),
        intent=(intent or "").strip(),
    )

    if isinstance(result, dict):
        return json.dumps(result)
    return json.dumps({"request_context": str(result)})


# ── Auxiliary HTTP routes ──────────────────────────────────────────────────────

@MCP_SERVER.custom_route("/", methods=["GET"], include_in_schema=False)
async def root(_: Request) -> JSONResponse:
    return JSONResponse({
        "service": SETTINGS.get("app.name", "acct-mgnt-mcp"),
        "message": "Use POST /mcp for the MCP transport and GET /health for service health.",
    })


@MCP_SERVER.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse(BRIDGE.health())


# streamable_http_app() handles the full MCP lifecycle:
# initialize → notifications/initialized → tools/list → tools/call
app = MCP_SERVER.streamable_http_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", os.getenv("SERVER_PORT", "8000")))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")