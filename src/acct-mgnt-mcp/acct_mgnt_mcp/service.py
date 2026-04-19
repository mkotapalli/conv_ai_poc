from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import boto3
import requests
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

SERVICE_ROOT = Path(__file__).resolve().parents[1]
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
        self.secret_overrides = self._load_secret_overrides()

    @staticmethod
    def _to_env_key(key: str) -> str:
        return key.upper().replace(".", "_").replace("-", "_")

    def _raw_lookup(self, key: str, default: str = "") -> str:
        env_key = self._to_env_key(key)
        return os.getenv(env_key, self.properties.get(key, default))

    def _load_secret_overrides(self) -> dict[str, str]:
        enabled_value = self._raw_lookup("aws.secretsmanager.enabled", "true").strip().lower()
        if enabled_value not in {"1", "true", "yes", "y", "on"}:
            return {}

        secret_name = self._raw_lookup("aws.secretsmanager.secret_name", "").strip()
        if not secret_name:
            return {}

        region = self._raw_lookup(
            "aws.secretsmanager.region",
            self._raw_lookup("aws.region", os.getenv("AWS_REGION", "us-east-1")),
        ).strip() or "us-east-1"

        try:
            client = boto3.session.Session(region_name=region).client("secretsmanager", region_name=region)
            response = client.get_secret_value(SecretId=secret_name)
            secret_payload = json.loads(response.get("SecretString", "{}"))
            if not isinstance(secret_payload, dict):
                return {}
            LOGGER.info(
                "Loaded secrets from AWS Secrets Manager '%s' with keys: %s",
                secret_name,
                sorted(secret_payload.keys()),
            )
        except Exception as exc:  # pragma: no cover - depends on AWS env
            LOGGER.warning("Failed to load AWS Secrets Manager overrides: %s", exc)
            return {}

        normalized = {str(key): "" if value is None else str(value) for key, value in secret_payload.items()}
        aliases = {
            "AWS_ACCESS_KEY_ID": ("AWS_ACCESS_KEY_ID", "aws_access_key_id", "accessKeyId", "access_key_id"),
            "AWS_SECRET_ACCESS_KEY": (
                "AWS_SECRET_ACCESS_KEY",
                "aws_secret_access_key",
                "secretAccessKey",
                "secret_access_key",
            ),
            "AWS_SESSION_TOKEN": ("AWS_SESSION_TOKEN", "aws_session_token", "sessionToken", "session_token"),
            "AWS_REGION": ("AWS_REGION", "aws_region", "region"),
            "AWS_DEFAULT_REGION": ("AWS_DEFAULT_REGION", "aws_default_region"),
        }
        for env_name, candidates in aliases.items():
            value = next((normalized.get(candidate) for candidate in candidates if normalized.get(candidate)), "")
            if value and not os.getenv(env_name):
                os.environ[env_name] = value
        return normalized

    def get(self, key: str, default: str = "") -> str:
        env_key = self._to_env_key(key)
        if env_key in os.environ:
            return os.environ[env_key]
        if env_key in self.secret_overrides:
            return self.secret_overrides[env_key]
        if key in self.secret_overrides:
            return self.secret_overrides[key]
        lowered_env_key = env_key.lower()
        if lowered_env_key in self.secret_overrides:
            return self.secret_overrides[lowered_env_key]
        return self.properties.get(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, str(default)).strip().lower()
        return value in {"1", "true", "yes", "y", "on"}

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
            "mcp_path": self.settings.get("service.agentcore.runtime.mcp_path", "/mcp"),
            "health_path": self.settings.get("service.agentcore.runtime.health_path", "/health"),
            "orchestrator_url": self.orchestrator_url,
            "runtime_protocol": self.settings.get("service.agentcore.runtime.protocol", "MCP"),
        }

    def invoke(self, *, message: str, conversation_id: str | None, user_context: dict[str, Any] | None) -> dict[str, Any]:
        if not self.orchestrator_url:
            raise RuntimeError("Orchestrator URL is missing from configuration.")

        normalized_message = message.strip()
        if not normalized_message:
            raise ValueError("A non-empty message is required.")

        payload = {
            "conversation_id": conversation_id or str(uuid.uuid4()),
            "message": normalized_message,
            "user_context": user_context or {},
        }
        payload_text = json.dumps(payload)
        headers = {"content-type": "application/json"}
        timeout_seconds = self.settings.get_int("http.timeout.seconds", 30)

        response = requests.post(
            self.orchestrator_url,
            data=payload_text,
            headers=headers,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict):
            result.setdefault("conversation_id", payload["conversation_id"])
        return result


SETTINGS = Settings(CONFIG_PATH)
BRIDGE = OrchestratorBridge(SETTINGS)
MCP_SERVER = FastMCP(
    name=SETTINGS.get("app.name", "acct-mgnt-mcp"),
    instructions=(
        "Expose account-management MCP tools for AgentCore Gateway. Use the orchestrator_invoke tool "
        "to route password-reset, unlock, and general account access requests to orchestrator-agent."
    ),
    host=SETTINGS.get("server.host", "0.0.0.0"),
    port=SETTINGS.get_int("server.port", 8083),
    streamable_http_path=SETTINGS.get("service.agentcore.runtime.mcp_path", "/mcp"),
    stateless_http=True,
    log_level="INFO",
)


@MCP_SERVER.resource(
    "account://password-reset",
    name="Password Reset",
    description="Guidance for password reset requests that should be routed through the orchestrator-agent.",
    mime_type="application/json",
)
def password_reset_resource() -> dict[str, Any]:
    return {
        "action": "password_reset",
        "description": "Handles user password reset requests through the orchestrator-agent.",
        "target": "orchestrator-agent",
        "tool": "orchestrator_invoke",
    }


@MCP_SERVER.resource(
    "account://account-unlock",
    name="Account Unlock",
    description="Guidance for account unlock requests that should be routed through the orchestrator-agent.",
    mime_type="application/json",
)
def account_unlock_resource() -> dict[str, Any]:
    return {
        "action": "account_unlock",
        "description": "Handles account unlock requests through the orchestrator-agent.",
        "target": "orchestrator-agent",
        "tool": "orchestrator_invoke",
    }


@MCP_SERVER.resource(
    "account://runtime-registration",
    name="AgentCore Runtime Registration",
    description="Runtime metadata used when registering this MCP service in AgentCore Runtime and Gateway.",
    mime_type="application/json",
)
def runtime_registration_resource() -> dict[str, Any]:
    return {
        "server_protocol": SETTINGS.get("service.agentcore.runtime.protocol", "MCP"),
        "mcp_path": SETTINGS.get("service.agentcore.runtime.mcp_path", "/mcp"),
        "health_path": SETTINGS.get("service.agentcore.runtime.health_path", "/health"),
        "container_port": SETTINGS.get_int("server.port", 8083),
    }


@MCP_SERVER.tool(
    name="orchestrator_invoke",
    title="Invoke Orchestrator Agent",
    description="Forward a user message from AgentCore Gateway to orchestrator-agent and return the orchestration result.",
)
def orchestrator_invoke(
    message: str,
    conversation_id: str | None = None,
    user_context: dict[str, Any] | None = None,
    user_query: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    normalized_message = (message or user_query or "").strip()
    normalized_user_context = dict(user_context or {})
    memory_id = SETTINGS.get("memory.agentcore.memory_id", "").strip()
    if memory_id and "memory_id" not in normalized_user_context:
        normalized_user_context["memory_id"] = memory_id
    if user_id and "user_id" not in normalized_user_context:
        normalized_user_context["user_id"] = user_id

    result = BRIDGE.invoke(
        message=normalized_message,
        conversation_id=conversation_id,
        user_context=normalized_user_context,
    )
    return {
        "status": result.get("status", "ok") if isinstance(result, dict) else "ok",
        "conversation_id": (result.get("conversation_id") if isinstance(result, dict) else None)
        or conversation_id,
        "delivery_mode": result.get("delivery_mode", "orchestrator") if isinstance(result, dict) else "orchestrator",
        "orchestrator_response": result,
    }


@MCP_SERVER.custom_route("/", methods=["GET"], include_in_schema=False)
async def root(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "service": SETTINGS.get("app.name", "acct-mgnt-mcp"),
            "message": "Use POST /mcp for the MCP transport and GET /health for service health.",
        }
    )


@MCP_SERVER.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse(BRIDGE.health())


app = MCP_SERVER.streamable_http_app()
