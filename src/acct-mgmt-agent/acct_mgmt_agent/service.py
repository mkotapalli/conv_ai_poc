from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

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
            print(
                f"Loaded secrets from AWS Secrets Manager '{secret_name}' with keys: {sorted(secret_payload.keys())}"
            )
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

    def render_history(self, conversation_id: str, limit: int = 6) -> str:
        recent = self._memory.get(conversation_id, [])[-limit:]
        return "\n".join(f"{item['role']}: {item['text']}" for item in recent)


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

    def guardrails_enabled(self) -> bool:
        return self.settings.get_bool("bedrock.guardrail.enabled", False) and bool(
            self.settings.get("bedrock.guardrail_id")
        )

    def _guardrail_config(self) -> dict[str, Any]:
        if not self.guardrails_enabled():
            return {}

        config: dict[str, Any] = {
            "guardrail_id": self.settings.get("bedrock.guardrail_id"),
            "guardrail_version": self.settings.get("bedrock.guardrail_version", "DRAFT"),
            "guardrail_trace": self.settings.get("bedrock.guardrail_trace", "enabled"),
            "guardrail_redact_input": self.settings.get_bool("bedrock.guardrail_redact_input", True),
            "guardrail_redact_output": self.settings.get_bool("bedrock.guardrail_redact_output", True),
        }
        stream_mode = self.settings.get("bedrock.guardrail_stream_processing_mode", "sync")
        if stream_mode:
            config["guardrail_stream_processing_mode"] = stream_mode
        return config

    def _block_message(self, fallback: str) -> str:
        return self.settings.get(
            "bedrock.guardrail_block_message",
            fallback or "The request was blocked by AWS Guardrails for safety reasons.",
        )

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
                **self._guardrail_config(),
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

    @staticmethod
    def _extract_text(result: Any) -> str:
        message = getattr(result, "message", result)
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content", [])

        parts: list[str] = []
        for block in content or []:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(str(text))

        if parts:
            return "\n".join(parts).strip()
        return str(result).strip()

    def ask_text(self, prompt: str, fallback: str) -> str:
        self._build_agent()
        if self._agent is None:
            return fallback
        try:
            result = self._agent(prompt)
            stop_reason = getattr(result, "stop_reason", "")
            if stop_reason in {"guardrail_intervened", "content_filtered"}:
                return self._block_message(fallback)
            text = self._extract_text(result)
            self.last_error = ""
            return text or fallback
        except Exception as exc:
            self._record_error(f"Failed to execute prompt with `{self.name}`", exc)
            return fallback

    def get_agent(self) -> Agent | None:
        self._build_agent()
        return self._agent


def validate_sigv4_headers(headers: Mapping[str, str], required: bool, expected_access_key: str = "") -> None:
    if not required:
        return

    authorization = headers.get("authorization", "")
    amz_date = headers.get("x-amz-date", "")
    if not authorization.startswith("AWS4-HMAC-SHA256") or not amz_date:
        raise ValueError("Inbound request is missing SigV4 authentication headers")

    if expected_access_key and f"Credential={expected_access_key}/" not in authorization:
        raise ValueError("The SigV4 access key is not trusted for this environment")


def validate_a2a_header(headers: Mapping[str, str], header_name: str = "", expected_value: str = "") -> None:
    normalized_name = header_name.strip().lower()
    if not normalized_name:
        return

    received_value = headers.get(normalized_name, "")
    if not received_value:
        raise ValueError("Inbound request is missing A2A authentication header")

    if expected_value and received_value != expected_value:
        raise ValueError("Inbound request failed A2A authentication")


class AccountManagementService:
    def __init__(self) -> None:
        self.settings = Settings(CONFIG_PATH)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        self.memory = AgentCoreMemoryStore(
            storage_path=self.settings.get("memory.storage_path", "data/account-memory.json"),
            provider=self.settings.get("memory.provider", "local"),
        )
        self.agent = StrandsResponder(self.settings, self.system_prompt, "acct-mgmt-agent")

    def health(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.settings.get("app.name", "acct-mgmt-agent"),
            "memory_provider": self.memory.provider,
            "memory_id": self.settings.get("memory.agentcore.memory_id", ""),
            "agent_last_error": self.agent.last_error,
            "guardrails_enabled": str(self.agent.guardrails_enabled()).lower(),
            "guardrail_id": self.settings.get("bedrock.guardrail_id", ""),
        }

    def validate_sigv4(self, headers: Mapping[str, str]) -> None:
        validate_a2a_header(
            headers,
            header_name=self.settings.get("auth.a2a.required_header", ""),
            expected_value=self.settings.get("auth.a2a.expected_value", ""),
        )
        validate_sigv4_headers(
            headers,
            required=self.settings.get_bool("auth.sigv4.required_header", False),
            expected_access_key=self.settings.get("auth.sigv4.trusted_access_key_id", ""),
        )

    @staticmethod
    def _fallback_answer(intent: str) -> str:
        if intent == "password_reset":
            return (
                "POC response: I can help with a password reset. In the company environment, "
                "this step would verify the user in Okta and trigger the reset workflow."
            )
        if intent == "password_unlock":
            return (
                "POC response: I can help unlock the account. In the company environment, "
                "this would call the unlock flow after identity verification."
            )
        return "I can assist with password reset or password unlock requests."

    def handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        conversation_id = payload.get("conversation_id") or "default-conversation"
        intent = str(payload.get("intent", "general")).strip().lower()
        message = str(payload.get("message", "")).strip()
        user_context = dict(payload.get("user_context") or {})
        memory_id = self.settings.get("memory.agentcore.memory_id", "").strip()
        if memory_id and "memory_id" not in user_context:
            user_context["memory_id"] = memory_id

        self.memory.append(conversation_id, "user", message)
        fallback = self._fallback_answer(intent)
        prompt = f"""
{self.system_prompt}

Conversation history:
{self.memory.render_history(conversation_id)}

User intent: {intent}
User message: {message}

Reply as a corporate IT account-management assistant in under 100 words.
""".strip()
        answer = self.agent.ask_text(prompt, fallback)
        self.memory.append(conversation_id, "assistant", answer)

        return {
            "status": "ok",
            "intent": intent,
            "answer": answer,
            "memory_provider": self.memory.provider,
            "memory_id": user_context.get("memory_id", ""),
            "agent_last_error": self.agent.last_error,
            "guardrails_enabled": self.agent.guardrails_enabled(),
        }
