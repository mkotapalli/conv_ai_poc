from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import boto3
from strands import Agent
from strands.models import BedrockModel

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.getenv("CONFIG_FILE", SERVICE_ROOT / "config" / "application.properties"))
PROMPT_PATH = Path(os.getenv("SYSTEM_PROMPT_FILE", SERVICE_ROOT / "prompts" / "system_prompt.txt"))
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
        except Exception:
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


class AgentCoreMemoryStore:
    def __init__(self, storage_path: str, provider: str = "local") -> None:
        self.provider = provider
        self.storage_path = Path(storage_path)
        if not self.storage_path.is_absolute():
            self.storage_path = SERVICE_ROOT / self.storage_path
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory = self._load()

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if not self.storage_path.exists():
            return {}
        try:
            return json.loads(self.storage_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self) -> None:
        self.storage_path.write_text(json.dumps(self._memory, indent=2), encoding="utf-8")

    def append(self, conversation_id: str, role: str, text: str) -> None:
        self._memory.setdefault(conversation_id, []).append({"role": role, "text": text})
        self._save()


class StrandsResponder:
    def __init__(self, settings: Settings, system_prompt: str, name: str) -> None:
        self.settings = settings
        self.system_prompt = system_prompt
        self.name = name
        self._agent: Agent | None = None
        self.last_error = ""

    def _record_error(self, context: str, exc: Exception) -> None:
        self.last_error = f"{context}: {exc.__class__.__name__}: {exc}"
        LOGGER.exception(self.last_error)

    def _build_agent(self) -> None:
        if self._agent is not None:
            return
        model_id = (
            self.settings.get("bedrock.inference_profile_id")
            or self.settings.get("bedrock.model_id", "amazon.nova-2-lite-v1:0")
        ).strip()
        region = self.settings.get("aws.region", "us-east-1")
        try:
            model = BedrockModel(
                model_id=model_id,
                region_name=region,
                temperature=0.2,
                max_tokens=self.settings.get_int("bedrock.max_tokens", 1024),
            )
            self._agent = Agent(
                name=self.name,
                description=self.settings.get(
                    "app.description",
                    "Account-management specialist for password reset and account unlock support.",
                ),
                model=model,
                system_prompt=self.system_prompt,
            )
            self.last_error = ""
        except Exception as exc:
            self._agent = None
            self._record_error(f"Failed to create agent `{self.name}`", exc)

    def get_agent(self) -> Agent | None:
        self._build_agent()
        return self._agent


class AccountManagementService:
    def __init__(self) -> None:
        self.settings = Settings(CONFIG_PATH)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip() if PROMPT_PATH.exists() else ""
        self.memory = AgentCoreMemoryStore(
            storage_path=self.settings.get("memory.storage_path", "data/account-memory.json"),
            provider=self.settings.get("memory.provider", "local"),
        )
        self.agent = StrandsResponder(self.settings, self.system_prompt, "acct-mgmt-agent")
        self._member_channel_map: dict[str, dict[str, Any]] = {}

    def get_a2a_agent(self) -> Agent | None:
        return self.agent.get_agent()

    def health(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.settings.get("app.name", "acct-mgmt-agent"),
            "memory_provider": self.memory.provider,
            "memory_id": self.settings.get("memory.agentcore.memory_id", ""),
            "mode": "stubbed_api",
            "a2a_enabled": str(self.settings.get_bool("service.a2a.enabled", True)).lower(),
            "a2a_agent_ready": str(self.get_a2a_agent() is not None).lower(),
            "agent_last_error": self.agent.last_error,
        }

    def _stub_member_channels(self, member_eid: str) -> dict[str, Any]:
        # Deterministic local stub behavior until real API integration is wired.
        last_digit = int(member_eid[-1]) if member_eid and member_eid[-1].isdigit() else 0
        account_found = bool(member_eid)
        account_status = "Locked" if last_digit in {5, 7, 9} else "Enabled"
        channels = {
            "account_found": account_found,
            "phone_available": account_found,
            "phone_type_mobile": account_found and last_digit % 2 == 0,
            "email_available": account_found,
            "account_status": account_status,
        }
        if member_eid:
            self._member_channel_map[member_eid] = channels
        return channels

    def handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        genesys_conversation_id = str(payload.get("genesys_conversation_id") or "").strip()
        gecx_session_id = str(payload.get("gecx_session_id") or "").strip()
        aie_session_id = str(payload.get("aie_session_id") or "").strip()
        request_type = str(payload.get("request_type") or "").strip()
        intent = str(payload.get("intent") or "").strip()
        member_eid = str(payload.get("member_eid") or "").strip()
        delivery_type = str(payload.get("delivery_type") or "").strip().lower()

        conversation_key = genesys_conversation_id or gecx_session_id or "default-conversation"
        self.memory.append(conversation_key, "user", json.dumps(payload, default=str))

        if request_type == "validate_account":
            channels = self._stub_member_channels(member_eid)
            response = {
                "genesys_conversation_id": genesys_conversation_id,
                "gecx_session_id": gecx_session_id,
                "aie_session_id": aie_session_id,
                "request_type": "validate_account",
                "account_found": channels["account_found"],
                "phone_available": channels["phone_available"],
                "phone_type_mobile": channels["phone_type_mobile"],
                "email_available": channels["email_available"],
                "account_status": channels["account_status"],
                "request_status": "Success" if channels["account_found"] else "Failure",
                "request_status_msg": "Account validated" if channels["account_found"] else "Member account not found",
            }
            self.memory.append(conversation_key, "assistant", json.dumps(response, default=str))
            return response

        if request_type == "pw_send_link":
            channels = self._member_channel_map.get(member_eid) or self._stub_member_channels(member_eid)
            if delivery_type not in {"sms", "email"}:
                response = {
                    "genesys_conversation_id": genesys_conversation_id,
                    "gecx_session_id": gecx_session_id,
                    "aie_session_id": aie_session_id,
                    "request_type": "pw_send_link",
                    "pw_link_sent": False,
                    "request_status": "Failure",
                    "request_status_msg": "Unsupported delivery_type. Use sms or email.",
                }
                self.memory.append(conversation_key, "assistant", json.dumps(response, default=str))
                return response

            channel_available = channels["phone_available"] if delivery_type == "sms" else channels["email_available"]
            response = {
                "genesys_conversation_id": genesys_conversation_id,
                "gecx_session_id": gecx_session_id,
                "aie_session_id": aie_session_id,
                "request_type": "pw_send_link",
                "pw_link_sent": bool(channel_available),
                "request_status": "Success" if channel_available else "Failure",
                "request_status_msg": "Password reset link sent" if channel_available else f"{delivery_type} delivery channel not available",
            }
            self.memory.append(conversation_key, "assistant", json.dumps(response, default=str))
            return response

        response = {
            "genesys_conversation_id": genesys_conversation_id,
            "gecx_session_id": gecx_session_id,
            "aie_session_id": aie_session_id,
            "request_type": request_type,
            "request_status": "Failure",
            "request_status_msg": f"Unsupported request_type: {request_type or 'missing'} for intent {intent or 'unknown'}",
        }
        self.memory.append(conversation_key, "assistant", json.dumps(response, default=str))
        return response
