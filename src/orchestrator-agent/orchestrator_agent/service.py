from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

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


def resolve_path(path_value: str, base_path: Path) -> Path:
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = (base_path / candidate).resolve()
    return candidate


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.properties = load_properties(path)

    def get(self, key: str, default: str = "") -> str:
        env_key = key.upper().replace(".", "_").replace("-", "_")
        return os.getenv(env_key, self.properties.get(key, default))

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

        model_id = self.settings.get(
            "bedrock.model_id", "amazon.nova-2-lite-v1:0"
        )
        region = self.settings.get("aws.region", "us-east-1")
        try:
            model = BedrockModel(
                model_id=model_id,
                region_name=region,
                temperature=0.1,
                max_tokens=512,
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


def validate_sigv4_headers(headers: Mapping[str, str], required: bool, expected_access_key: str = "") -> None:
    if not required:
        return

    authorization = headers.get("authorization", "")
    amz_date = headers.get("x-amz-date", "")
    if not authorization.startswith("AWS4-HMAC-SHA256") or not amz_date:
        raise ValueError("Inbound request is missing SigV4 authentication headers")

    if expected_access_key and f"Credential={expected_access_key}/" not in authorization:
        raise ValueError("The SigV4 access key is not trusted for this environment")


class StrandsA2ADelegate:
    def __init__(self, settings: Settings, orchestrator_prompt: str) -> None:
        self.settings = settings
        self.last_error = ""
        self.tool_name = self.settings.get("service.account_agent.tool_name", "acct_mgmt_agent")
        self.account_prompt = self._load_account_prompt()
        self.account_settings = self._load_account_settings()
        self.account_responder = StrandsResponder(self.account_settings, self.account_prompt, "acct-mgmt-agent")
        router_prompt = (
            f"{orchestrator_prompt}\n\n"
            "When the user needs password reset or password unlock help, delegate to the "
            f"`{self.tool_name}` tool before you reply. Keep the final answer short and enterprise-safe."
        )
        self.router_responder = StrandsResponder(settings, router_prompt, "orchestrator-a2a-router")
        self._router_agent: Agent | None = None

    def mode(self) -> str:
        return self.settings.get("service.account_agent.invoke_mode", "strands_a2a").strip().lower()

    def enabled(self) -> bool:
        return self.mode() == "strands_a2a"

    def _load_account_settings(self) -> Settings:
        default_path = SERVICE_ROOT.parent / "acct-mgmt-agent" / "config" / "application.properties"
        configured_path = self.settings.get("service.account_agent.config_path", str(default_path))
        resolved_path = resolve_path(configured_path, SERVICE_ROOT)
        if resolved_path.exists():
            return Settings(resolved_path)
        return self.settings

    def _load_account_prompt(self) -> str:
        default_path = SERVICE_ROOT.parent / "acct-mgmt-agent" / "prompts" / "system_prompt.txt"
        configured_path = self.settings.get("service.account_agent.prompt_path", str(default_path))
        resolved_path = resolve_path(configured_path, SERVICE_ROOT)
        if resolved_path.exists():
            return resolved_path.read_text(encoding="utf-8").strip()
        return (
            "You are the account-management support agent for a corporate conversation AI solution on AWS. "
            "Help with password reset and password unlock requests, and do not claim backend success unless confirmed."
        )

    def _build_router_agent(self) -> None:
        if self._router_agent is not None:
            return

        account_agent = self.account_responder.create_agent(
            description="Specialist agent for password reset and password unlock requests.",
            force_new=True,
        )
        if account_agent is None:
            self.last_error = self.account_responder.last_error or "The account-management agent could not be created."
            return

        account_tool = account_agent.as_tool(
            name=self.tool_name,
            description=(
                "Invoke the account-management agent for employee password reset or password unlock issues."
            ),
            preserve_context=True,
        )
        self._router_agent = self.router_responder.create_agent(
            description="Coordinates the handoff from the orchestrator to specialist support agents.",
            tools=[account_tool],
            force_new=True,
        )
        if self._router_agent is None:
            self.last_error = self.router_responder.last_error or "The Strands A2A router agent could not be created."
        else:
            self.last_error = ""

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            return {"status": "skipped", "delivery_mode": "strands_a2a_required"}

        self._build_router_agent()
        intent = str(payload.get("intent", "general"))
        message = str(payload.get("message", "")).strip()
        user_context = payload.get("user_context") or {}
        fallback = (
            f"POC A2A response: your `{intent}` request has been handed to the account-management agent. "
            "In production, this would continue through the secured identity workflow."
        )
        prompt = f"""
Use Strands A2A handoff for this request.

Intent: {intent}
Conversation ID: {payload.get("conversation_id", "")}
User context: {json.dumps(user_context, default=str)}
User message: {message}

Always call the `{self.tool_name}` tool exactly once for password reset or password unlock requests,
then return the final user-facing answer in plain text only.
""".strip()

        if self._router_agent is None:
            answer = self.account_responder.ask_text(prompt, fallback)
            error_detail = self.last_error or self.account_responder.last_error or "The Strands A2A router agent is unavailable."
            return {
                "status": "ok",
                "answer": answer,
                "delivery_mode": "strands_a2a_fallback",
                "a2a_error": error_detail,
            }

        try:
            result = self._router_agent(prompt)
            stop_reason = getattr(result, "stop_reason", "")
            if stop_reason in {"guardrail_intervened", "content_filtered"}:
                answer = self.account_responder._block_message(fallback)
            else:
                answer = StrandsResponder._extract_text(result) or fallback
            return {"status": "ok", "answer": answer, "delivery_mode": "strands_a2a"}
        except Exception as exc:
            self.last_error = f"Failed to execute the Strands A2A router: {exc.__class__.__name__}: {exc}"
            LOGGER.exception(self.last_error)
            return {
                "status": "degraded",
                "answer": f"{fallback} Detail: {exc}",
                "delivery_mode": "strands_a2a_error",
                "a2a_error": self.last_error,
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
        self.a2a_delegate = StrandsA2ADelegate(self.settings, self.system_prompt)

    def health(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.settings.get("app.name", "orchestrator-agent"),
            "memory_provider": self.memory.provider,
            "a2a_tool": self.settings.get("service.account_agent.tool_name", "acct_mgmt_agent"),
            "account_agent_invoke_mode": self.a2a_delegate.mode(),
            "a2a_last_error": self.a2a_delegate.last_error,
            "agent_last_error": self.agent.last_error,
            "guardrails_enabled": str(self.agent.guardrails_enabled()).lower(),
            "guardrail_id": self.settings.get("bedrock.guardrail_id", ""),
        }

    def validate_sigv4(self, headers: Mapping[str, str]) -> None:
        validate_sigv4_headers(
            headers,
            required=self.settings.get_bool("auth.sigv4.required_header", False),
            expected_access_key=self.settings.get("auth.sigv4.trusted_access_key_id", ""),
        )

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
        if "unlock" in text:
            return "password_unlock"
        if "reset" in text or "forgot" in text:
            return "password_reset"
        return "general"

    def identify_intent(self, message: str, conversation_id: str) -> dict[str, Any]:
        fallback_intent = self._fallback_intent(message)
        prompt = f"""
{self.system_prompt}

Conversation history:
{self.memory.render_history(conversation_id)}

User message: {message}

Classify the intent into one of these values only:
- password_reset
- password_unlock
- general

Return strict JSON only, for example:
{{"intent": "password_reset", "confidence": 0.93}}
""".strip()

        result = self.agent.ask_json(prompt, {"intent": fallback_intent, "confidence": 0.75})
        return {
            "intent": self._normalize_intent(result.get("intent", fallback_intent)),
            "confidence": result.get("confidence", 0.75),
        }

    def general_response(self, message: str, conversation_id: str) -> str:
        history = self.memory.render_history(conversation_id)
        fallback = (
            "I can help route password reset and password unlock requests. "
            "Please tell me whether you need a reset or an unlock."
        )
        prompt = f"""
{self.system_prompt}

Conversation history:
{history}

User message: {message}

Reply in under 80 words and keep the answer appropriate for a corporate IT support assistant.
""".strip()
        return self.agent.ask_text(prompt, fallback)

    def call_account_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        a2a_response = self.a2a_delegate.invoke(payload)
        if a2a_response.get("status") == "ok":
            return a2a_response

        return {
            "status": "degraded",
            "delivery_mode": "strands_a2a_required",
            "answer": (
                "The account-management agent could not be reached through the mandatory Strands A2A path. "
                f"Detail: {a2a_response.get('answer', 'No downstream response returned.')}"
            ),
        }

    def route_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        conversation_id = payload.get("conversation_id") or str(uuid.uuid4())
        message = str(payload.get("message", "")).strip()
        user_context = payload.get("user_context") or {}

        self.memory.append(conversation_id, "user", message, metadata={"user": user_context})
        intent_info = self.identify_intent(message, conversation_id)
        intent = intent_info["intent"]

        delivery_mode = "direct"
        if intent in {"password_reset", "password_unlock"}:
            downstream_response = self.call_account_agent(
                {
                    "conversation_id": conversation_id,
                    "message": message,
                    "intent": intent,
                    "user_context": user_context,
                }
            )
            answer = downstream_response.get("answer", "The account-management agent returned no response.")
            routed_to = "acct-mgmt-agent"
            delivery_mode = downstream_response.get("delivery_mode", self.a2a_delegate.mode())
        else:
            answer = self.general_response(message, conversation_id)
            routed_to = "orchestrator-agent"

        self.memory.append(
            conversation_id,
            "assistant",
            answer,
            metadata={"intent": intent, "routed_to": routed_to, "delivery_mode": delivery_mode},
        )

        return {
            "status": "ok",
            "conversation_id": conversation_id,
            "intent": intent,
            "confidence": intent_info.get("confidence", 0.75),
            "routed_to": routed_to,
            "delivery_mode": delivery_mode,
            "answer": answer,
            "memory_provider": self.memory.provider,
            "guardrails_enabled": self.agent.guardrails_enabled(),
            "user": user_context.get("sub", "anonymous"),
        }
