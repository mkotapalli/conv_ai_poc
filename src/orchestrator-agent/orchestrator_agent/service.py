from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from strands import Agent, tool
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

    def append(self, conversation_id: str, role: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        self._memory.setdefault(conversation_id, []).append(
            {"role": role, "text": text, "metadata": metadata or {}}
        )
        self._save()

    def history(self, conversation_id: str, limit: int = 6) -> list[dict[str, Any]]:
        return self._memory.get(conversation_id, [])[-limit:]

    def render_history(self, conversation_id: str, limit: int = 6) -> str:
        return "\n".join(
            f"{item['role']}: {item['text']}" for item in self.history(conversation_id, limit=limit)
        )


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

    def create_agent(
        self,
        *,
        description: str | None = None,
        tools: list[Any] | None = None,
        force_new: bool = False,
    ) -> Agent | None:
        if self._agent is not None and not force_new and not tools and description is None:
            return self._agent

        model_id = (
            self.settings.get("bedrock.inference_profile_id")
            or self.settings.get("bedrock.model_id", "amazon.nova-2-lite-v1:0")
        ).strip()
        region = self.settings.get("aws.region", "us-east-1")
        try:
            model = BedrockModel(
                model_id=model_id,
                region_name=region,
                temperature=0.1,
                max_tokens=self.settings.get_int("bedrock.max_tokens", 1024),
                **self._guardrail_config(),
            )
            agent = Agent(
                name=self.name,
                description=description,
                model=model,
                system_prompt=self.system_prompt,
                tools=tools or [],
            )
            if not force_new and not tools and description is None:
                self._agent = agent
            self.last_error = ""
            return agent
        except Exception as exc:
            self._record_error(f"Failed to create agent `{self.name}`", exc)
            return None

    @staticmethod
    def _extract_text(result: Any) -> str:
        message = getattr(result, "message", result)
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content", [])

        blocks = content or []
        parts: list[str] = []
        for block in blocks:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(str(text))

        if parts:
            return "\n".join(parts).strip()
        return str(result).strip()

    def ask_text(self, prompt: str, fallback: str) -> str:
        agent = self.create_agent()
        if agent is None:
            return fallback
        try:
            result = agent(prompt)
            stop_reason = getattr(result, "stop_reason", "")
            if stop_reason in {"guardrail_intervened", "content_filtered"}:
                return self._block_message(fallback)
            text = self._extract_text(result)
            self.last_error = ""
            return text or fallback
        except Exception as exc:
            self._record_error(f"Failed to execute prompt with `{self.name}`", exc)
            return fallback

    def ask_json(self, prompt: str, default: dict[str, Any]) -> dict[str, Any]:
        raw = self.ask_text(prompt, json.dumps(default))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    return default
        return default

    def ask_with_tools(self, prompt: str, tools: list, fallback: str) -> str:
        agent = self.create_agent(tools=tools)
        if agent is None:
            return fallback
        try:
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


class AccountAgentRemoteDelegate:
    """Invokes the acct-mgmt-agent via SigV4-signed POST to its AgentCore invocations URL."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error = ""

    def enabled(self) -> bool:
        return bool(self.endpoint())

    def endpoint(self) -> str:
        return self.settings.get("service.account_agent.invocations_url", "").strip()

    def _signed_headers(self, url: str, body: bytes) -> dict[str, str]:
        region = self.settings.get("aws.region", "us-east-1")
        session = boto3.Session(region_name=region)
        credentials = session.get_credentials().get_frozen_credentials()
        if credentials is None:
            raise RuntimeError("AWS credentials are not available for SigV4 signing")
        aws_request = AWSRequest(
            method="POST",
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(aws_request)
        return dict(aws_request.headers)

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            return {
                "status": "skipped",
                "answer": "Account agent invocations URL not configured (SERVICE_ACCOUNT_AGENT_INVOCATIONS_URL).",
            }

        url = self.endpoint()
        body = json.dumps(payload).encode("utf-8")
        timeout_seconds = self.settings.get_int("http.timeout.seconds", 20)
        try:
            headers = self._signed_headers(url, body)
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(url, content=body, headers=headers)
                response.raise_for_status()
                result = response.json()

            if isinstance(result, dict):
                result.setdefault("delivery_mode", "sigv4_invocation")
                self.last_error = ""
                return result

            raise RuntimeError(f"Non-object response from account agent: {result}")
        except Exception as exc:
            self.last_error = f"Failed to invoke account agent: {exc.__class__.__name__}: {exc}"
            LOGGER.exception(self.last_error)
            return {
                "status": "degraded",
                "answer": f"The account-management agent could not be reached. Detail: {exc}",
                "delivery_mode": "sigv4_invocation_error",
                "invoke_error": self.last_error,
            }


class OrchestratorService:
    def __init__(self) -> None:
        self.settings = Settings(CONFIG_PATH)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        self.memory = AgentCoreMemoryStore(
            storage_path=self.settings.get("memory.storage_path", "data/orchestrator-memory.json"),
            provider=self.settings.get("memory.provider", "local"),
        )
        self.agent = StrandsResponder(self.settings, self.system_prompt, "orchestrator-agent")
        self.remote_delegate = AccountAgentRemoteDelegate(self.settings)

    def health(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.settings.get("app.name", "orchestrator-agent"),
            "memory_provider": self.memory.provider,
            "memory_id": self.settings.get("memory.agentcore.memory_id", ""),
            "account_agent_invocations_url": self.remote_delegate.endpoint(),
            "account_agent_last_error": self.remote_delegate.last_error,
            "agent_last_error": self.agent.last_error,
            "guardrails_enabled": str(self.agent.guardrails_enabled()).lower(),
            "guardrail_id": self.settings.get("bedrock.guardrail_id", ""),
        }

    @staticmethod
    def _fallback_intent(message: str) -> str:
        lowered = message.lower()
        if "unlock" in lowered or "locked out" in lowered:
            return "password_unlock"
        if "reset" in lowered or "forgot" in lowered:
            return "password_reset"
        return "general"

    @staticmethod
    def _normalize_intent(raw_value: Any) -> str:
        text = str(raw_value).strip().lower().replace(" ", "_")
        if text in {"account_pw_reset", "account_password_reset", "password_reset", "reset_password"}:
            return "Account_PW_Reset"
        if text in {"account_unlock", "password_unlock", "unlock_account", "unlock"}:
            return "Account_Unlock"
        if "unlock" in text:
            return "Account_Unlock"
        if "reset" in text or "forgot" in text:
            return "Account_PW_Reset"
        return "General"

    def identify_intent(self, request_context: str, conversation_id: str) -> dict[str, Any]:
        fallback_intent = self._normalize_intent(self._fallback_intent(request_context))
        prompt = f"""
{self.system_prompt}

Conversation history:
{self.memory.render_history(conversation_id)}

User message: {request_context}

Classify the intent into one of these values only:
- Account_PW_Reset
- Account_Unlock
- General

Return strict JSON only, for example:
{{"intent": "Account_PW_Reset", "confidence": 0.93}}
""".strip()

        result = self.agent.ask_json(prompt, {"intent": fallback_intent, "confidence": 0.75})
        return {
            "intent": self._normalize_intent(result.get("intent", fallback_intent)),
            "confidence": result.get("confidence", 0.75),
        }

    def general_response(self, request_context: str, conversation_id: str) -> str:
        history = self.memory.render_history(conversation_id)
        fallback = (
            "I can help route password reset and password unlock requests. "
            "Please tell me whether you need a reset or an unlock."
        )
        prompt = f"""
{self.system_prompt}

Conversation history:
{history}

User message: {request_context}

Reply in under 80 words and keep the answer appropriate for a corporate IT support assistant.
""".strip()
        return self.agent.ask_text(prompt, fallback)

    def route_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        conversation_id = payload.get("conversation_id") or str(uuid.uuid4())
        request_context = str(payload.get("request_context") or payload.get("message", "")).strip()
        member_context = dict(payload.get("member_context") or {})
        user_context = dict(payload.get("user_context") or {})
        memory_id = self.settings.get("memory.agentcore.memory_id", "").strip()
        if memory_id and "memory_id" not in user_context:
            user_context["memory_id"] = memory_id

        self.memory.append(conversation_id, "user", request_context, metadata={"user": user_context})

        account_result: dict[str, Any] = {}

        @tool
        def account_management(
            intent: str,
            request_context: str,
            member_context_json: str = "{}",
        ) -> str:
            """Route account reset/unlock requests to the account-management runtime."""
            try:
                member_ctx = json.loads(member_context_json)
            except (json.JSONDecodeError, ValueError):
                member_ctx = {}

            result = self.remote_delegate.invoke(
                {
                    "intent": intent,
                    "conversation_id": conversation_id,
                    "member_context": {**member_context, **member_ctx},
                    "request_context": request_context,
                    "user_context": user_context,
                }
            )
            account_result.update(result)
            return json.dumps(result, default=str)

        provided_intent = str(payload.get("intent", "")).strip()
        intent_hint = (
            f"Intent provided by caller: {self._normalize_intent(provided_intent)}\n\n"
            if provided_intent
            else ""
        )
        history = self.memory.render_history(conversation_id)
        prompt = (
            f"{intent_hint}"
            f"Conversation history:\n{history}\n\n"
            f"User request: {request_context}\n\n"
            f"Member context available:\n{json.dumps(member_context, indent=2)}\n\n"
            "If the intent is Account_Unlock or Account_PW_Reset, call the account_management tool. "
            "Pass the normalised intent, the user's request_context, and the "
            "full member context serialised as JSON in the member_context_json parameter.\n"
            "If the intent is General or unclear, respond directly without calling any tool."
        ).strip()

        fallback = "I was unable to process your request at this time. Please try again."
        response_text = self.agent.ask_with_tools(prompt, tools=[account_management], fallback=fallback)

        if account_result:
            account_result.setdefault("conversation_id", conversation_id)
            account_result.setdefault("request_context", response_text or request_context)
            self.memory.append(
                conversation_id,
                "assistant",
                json.dumps(account_result, default=str),
                metadata={"routed_to": "acct-mgmt-agent", "delivery_mode": "strands_multiagent"},
            )
            return account_result

        response_payload = {
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
                    "failure_reason": "unsupported intent for account actions",
                }
            ],
            "request_context": response_text,
        }
        self.memory.append(
            conversation_id,
            "assistant",
            response_text,
            metadata={"intent": "General", "delivery_mode": "direct"},
        )
        return response_payload
