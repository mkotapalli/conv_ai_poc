from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
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

    def ask_with_tools(self, prompt: str, tools: list, fallback: str) -> str:
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
            agent = Agent(
                name=self.name,
                model=model,
                system_prompt=self.system_prompt,
                tools=tools,
            )
            result = agent(prompt)
            stop_reason = getattr(result, "stop_reason", "")
            if stop_reason in {"guardrail_intervened", "content_filtered"}:
                return self._block_message(fallback)
            text = self._extract_text(result)
            self.last_error = ""
            return text or fallback
        except Exception as exc:
            self._record_error(f"Failed to execute prompt with tools in `{self.name}`", exc)
            return fallback

    def get_agent(self) -> Agent | None:
        self._build_agent()
        return self._agent


class AccountApiMcpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error = ""

    def endpoint(self) -> str:
        return self.settings.get("service.account_api_mcp.url", "").strip()

    def enabled(self) -> bool:
        return bool(self.endpoint())

    def _signed_headers(self, url: str, body: bytes, headers: dict[str, str]) -> dict[str, str]:
        region = self.settings.get("aws.region", "us-east-1")
        session = boto3.Session(region_name=region)
        credentials = session.get_credentials().get_frozen_credentials()
        if credentials is None:
            raise RuntimeError("AWS credentials are not available for SigV4 signing")
        aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(aws_request)
        return dict(aws_request.headers)

    @staticmethod
    def _parse_mcp_result(response: requests.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        text = response.text.strip()

        if "text/event-stream" in content_type or text.startswith("event:"):
            data_line = next((line for line in text.splitlines() if line.startswith("data:")), "")
            if not data_line:
                raise ValueError(f"No data line in MCP SSE response: {text}")
            payload = json.loads(data_line[len("data:"):].strip())
        else:
            payload = response.json()

        content_items = payload.get("result", {}).get("content", [])
        if not content_items:
            raise ValueError(f"Unexpected MCP tools/call response: {payload}")

        raw_text = str(content_items[0].get("text", "")).strip()
        if not raw_text:
            raise ValueError(f"Empty MCP tools/call result content: {payload}")

        result = json.loads(raw_text)
        if not isinstance(result, dict):
            raise ValueError(f"MCP tool response must be a JSON object: {result}")
        return result

    def lookup(self, intent: str, contract_number: str, conversation_id: str) -> dict[str, Any]:
        url = self.endpoint()
        if not url:
            return {
                "found": False,
                "intent": intent,
                "contractNumber": contract_number,
                "api_response": "account not found",
                "error": "service.account_api_mcp.url not configured",
            }

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "account_api_lookup",
                "arguments": {
                    "intent": intent,
                    "contract_number": contract_number,
                },
            },
            "id": conversation_id,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        try:
            timeout_seconds = self.settings.get_int("http.timeout.seconds", 20)
            if "bedrock-agentcore" in url:
                headers = self._signed_headers(url, body, headers)
            response = requests.post(url, data=body, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            result = self._parse_mcp_result(response)
            result.setdefault("found", False)
            result.setdefault("intent", intent)
            result.setdefault("contractNumber", contract_number)
            result.setdefault("api_response", "account not found")
            self.last_error = ""
            return result
        except Exception as exc:
            self.last_error = f"Account API MCP call failed: {exc.__class__.__name__}: {exc}"
            LOGGER.exception(self.last_error)
            return {
                "found": False,
                "intent": intent,
                "contractNumber": contract_number,
                "api_response": "account not found",
                "error": self.last_error,
            }


class AccountManagementService:
    def __init__(self) -> None:
        self.settings = Settings(CONFIG_PATH)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        self.memory = AgentCoreMemoryStore(
            storage_path=self.settings.get("memory.storage_path", "data/account-memory.json"),
            provider=self.settings.get("memory.provider", "local"),
        )
        self.agent = StrandsResponder(self.settings, self.system_prompt, "acct-mgmt-agent")
        self.account_api = AccountApiMcpClient(self.settings)

    def health(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.settings.get("app.name", "acct-mgmt-agent"),
            "memory_provider": self.memory.provider,
            "memory_id": self.settings.get("memory.agentcore.memory_id", ""),
            "account_api_mcp_url": self.account_api.endpoint(),
            "account_api_mcp_last_error": self.account_api.last_error,
            "agent_last_error": self.agent.last_error,
            "guardrails_enabled": str(self.agent.guardrails_enabled()).lower(),
            "guardrail_id": self.settings.get("bedrock.guardrail_id", ""),
        }

    @staticmethod
    def _normalize_intent(raw_intent: str) -> str:
        normalized = raw_intent.strip().lower().replace(" ", "_")
        if normalized in {"account_pw_reset", "account_reset", "password_reset", "reset_password"}:
            return "Account_PW_Reset"
        if normalized in {"account_unlock", "password_unlock", "unlock_account"}:
            return "Account_Unlock"
        return "General"

    @staticmethod
    def _member_context_complete(member_context: dict[str, Any]) -> bool:
        required_keys = [
            "contractNumber",
            "birthDate",
            "zip",
            "eid",
            "groupNumber",
            "groupSuffix",
        ]
        return all(str(member_context.get(key, "")).strip() for key in required_keys)

    @staticmethod
    def _build_response_text(action: str, status: str, failure_reason: str) -> str:
        """Build a human-readable request_context string from action/status/failure_reason."""
        label = action.replace("_", " ").capitalize() if action else "Action"
        if status == "success":
            return f"{label} completed successfully."
        reason = failure_reason.strip() if failure_reason else "an unknown error occurred"
        return f"{label} failed: {reason}"

    def handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        conversation_id = payload.get("conversation_id") or "default-conversation"
        intent = self._normalize_intent(str(payload.get("intent", "General")))
        request_context = str(payload.get("request_context") or payload.get("message", "")).strip()
        member_context = dict(payload.get("member_context") or {})
        user_context = dict(payload.get("user_context") or {})
        memory_id = self.settings.get("memory.agentcore.memory_id", "").strip()
        if memory_id and "memory_id" not in user_context:
            user_context["memory_id"] = memory_id

        self.memory.append(conversation_id, "user", request_context)

        if intent not in {"Account_Unlock", "Account_PW_Reset"}:
            response = {
                "intent": "General",
                "conversation_id": conversation_id,
                "account_found": "no",
                "account_locked_at_start": "no",
                "account_locked_at_end": "no",
                "password_reset": "no",
                "action_detail": [
                    {
                        "action": "route_request",
                        "status": "failure",
                        "failure_reason": "unsupported intent for account action flow",
                    }
                ],
                "request_context": request_context,
            }
            self.memory.append(conversation_id, "assistant", json.dumps(response, default=str))
            return response

        contract_number = str(
            member_context.get("contractNumber")
            or payload.get("contractNumber")
            or ""
        ).strip()
        api_result = self.account_api.lookup(intent, contract_number, conversation_id)

        api_action = api_result.get("action") or ("unlock_account" if intent == "Account_Unlock" else "reset_password")
        api_status = api_result.get("status") or "failure"
        api_failure_reason = api_result.get("failure_reason") or ""
        response_text = self._build_response_text(api_action, api_status, api_failure_reason)

        if not bool(api_result.get("found")):
            response = {
                "intent": intent,
                "conversation_id": conversation_id,
                "account_found": "no",
                "account_locked_at_start": "no",
                "account_locked_at_end": "no",
                "password_reset": "no",
                "action_detail": [
                    {
                        "action": api_action,
                        "status": api_status,
                        "failure_reason": api_failure_reason or "account not found",
                    }
                ],
                "request_context": response_text,
            }
            self.memory.append(conversation_id, "assistant", json.dumps(response, default=str))
            return response

        if intent == "Account_Unlock":
            account_locked_at_start = "yes"
            account_locked_at_end = "no" if api_status == "success" else "yes"
            password_reset = "no"
        else:
            account_locked_at_start = "no"
            account_locked_at_end = "no"
            password_reset = "yes" if api_status == "success" else "no"

        response = {
            "intent": intent,
            "conversation_id": conversation_id,
            "account_found": "yes",
            "account_locked_at_start": account_locked_at_start,
            "account_locked_at_end": account_locked_at_end,
            "password_reset": password_reset,
            "action_detail": [
                {
                    "action": api_action,
                    "status": api_status,
                    "failure_reason": api_failure_reason,
                }
            ],
            "request_context": response_text,
        }
        self.memory.append(conversation_id, "assistant", json.dumps(response, default=str))
        return response
