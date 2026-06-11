from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from bedrock_agentcore.memory.constants import ConversationalMessage, MessageRole
from bedrock_agentcore.memory.session import MemorySessionManager
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

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, str(default)).strip().lower()
        return value in {"1", "true", "yes", "y", "on"}


class SessionStateStore:
    STATE_PREFIX = "STATE::"

    def __init__(self, storage_path: str, provider: str = "local", memory_id: str = "", region: str = "us-east-1") -> None:
        self.memory_id = memory_id.strip()
        self.region = region
        self.provider = "agentcore" if self.memory_id else provider
        self.storage_path = Path(storage_path)
        if not self.storage_path.is_absolute():
            self.storage_path = SERVICE_ROOT / self.storage_path
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory = self._load() if not self.uses_agentcore else {}
        self._manager = MemorySessionManager(memory_id=self.memory_id, region_name=self.region) if self.uses_agentcore else None

    @property
    def uses_agentcore(self) -> bool:
        return bool(self.memory_id)

    @staticmethod
    def _session_key(actor_id: str, session_id: str) -> str:
        return f"{actor_id}:{session_id}"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.storage_path.exists():
            return {}
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _save(self) -> None:
        self.storage_path.write_text(json.dumps(self._memory, indent=2), encoding="utf-8")

    def _agentcore_session(self, actor_id: str, session_id: str):
        if not self._manager:
            raise RuntimeError("AgentCore memory manager is not configured")
        return self._manager.create_memory_session(actor_id=actor_id, session_id=session_id)

    @staticmethod
    def _extract_state_from_event(event: Any) -> dict[str, Any] | None:
        payload = event.get("payload", []) if hasattr(event, "get") else getattr(event, "payload", [])
        for payload_item in reversed(payload or []):
            conversational = payload_item.get("conversational") if isinstance(payload_item, dict) else None
            if not conversational:
                continue
            text = str(conversational.get("content", {}).get("text", ""))
            if text.startswith(SessionStateStore.STATE_PREFIX):
                raw_state = text[len(SessionStateStore.STATE_PREFIX) :].strip()
                try:
                    state = json.loads(raw_state)
                except json.JSONDecodeError:
                    return None
                return state if isinstance(state, dict) else None
        return None

    def load(self, actor_id: str, session_id: str) -> dict[str, Any] | None:
        if self.uses_agentcore and self._manager:
            try:
                events = self._manager.list_events(actor_id=actor_id, session_id=session_id, max_results=100, include_payload=True)
                for event in reversed(events):
                    state = self._extract_state_from_event(event)
                    if state is not None:
                        return state
            except Exception as exc:
                LOGGER.exception("Failed to load session state from AgentCore Memory: %s", exc)
                return None

        return self._memory.get(self._session_key(actor_id, session_id))

    def save(self, actor_id: str, session_id: str, state: dict[str, Any]) -> None:
        if self.uses_agentcore and self._manager:
            try:
                session = self._agentcore_session(actor_id, session_id)
                payload = f"{self.STATE_PREFIX}{json.dumps(state, sort_keys=True)}"
                session.add_turns([ConversationalMessage(payload, MessageRole.OTHER)])
                return
            except Exception as exc:
                LOGGER.exception("Failed to persist session state to AgentCore Memory: %s", exc)
                return

        self._memory[self._session_key(actor_id, session_id)] = state
        self._save()

    def session_exists(self, actor_id: str, session_id: str) -> bool:
        return self.load(actor_id, session_id) is not None


class AccountAgentRemoteDelegate:
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
        credentials_provider = session.get_credentials()
        if credentials_provider is None:
            raise RuntimeError("AWS credentials are not available for SigV4 signing")
        credentials = credentials_provider.get_frozen_credentials()
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
            self.last_error = "Account agent invocations URL not configured"
            return {
                "request_status": "Failure",
                "request_status_msg": self.last_error,
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
                self.last_error = ""
                return result

            raise RuntimeError(f"Non-object response from account agent: {result}")
        except Exception as exc:
            self.last_error = f"Failed to invoke account agent: {exc.__class__.__name__}: {exc}"
            LOGGER.exception(self.last_error)
            return {
                "request_status": "Failure",
                "request_status_msg": "The account-management agent could not be reached",
                "invoke_error": self.last_error,
            }


class StrandsResponder:
    def __init__(self, settings: Settings, system_prompt: str, name: str) -> None:
        self.settings = settings
        self.system_prompt = system_prompt
        self.name = name
        self.last_error = ""

    def _record_error(self, context: str, exc: Exception) -> None:
        self.last_error = f"{context}: {exc.__class__.__name__}: {exc}"
        LOGGER.exception(self.last_error)

    def _build_model(self) -> BedrockModel:
        model_id = (
            self.settings.get("bedrock.inference_profile_id")
            or self.settings.get("bedrock.model_id", "amazon.nova-2-lite-v1:0")
        ).strip()
        region = self.settings.get("aws.region", "us-east-1")
        return BedrockModel(
            model_id=model_id,
            region_name=region,
            temperature=0.1,
            max_tokens=self.settings.get_int("bedrock.max_tokens", 512),
        )

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

    def ask_with_tools(self, prompt: str, tools: list, fallback: str) -> str:
        try:
            model = self._build_model()
            agent = Agent(
                name=self.name,
                model=model,
                system_prompt=self.system_prompt,
                tools=tools,
            )
            result = agent(prompt)
            text = self._extract_text(result)
            self.last_error = ""
            return text or fallback
        except Exception as exc:
            self._record_error(f"Failed to execute prompt with tools in `{self.name}`", exc)
            return fallback


class OrchestratorService:
    def __init__(self) -> None:
        self.settings = Settings(CONFIG_PATH)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip() if PROMPT_PATH.exists() else ""
        self.session_store = SessionStateStore(
            storage_path=self.settings.get("memory.storage_path", "data/orchestrator-memory.json"),
            provider=self.settings.get("memory.provider", "local"),
            memory_id=self.settings.get("memory.agentcore.memory_id", ""),
            region=self.settings.get("aws.region", "us-east-1"),
        )
        self.agent = StrandsResponder(self.settings, self.system_prompt, "orchestrator-agent")
        self.remote_delegate = AccountAgentRemoteDelegate(self.settings)

    def health(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.settings.get("app.name", "orchestrator-agent"),
            "memory_provider": self.session_store.provider,
            "memory_id": self.settings.get("memory.agentcore.memory_id", ""),
            "account_agent_invocations_url": self.remote_delegate.endpoint(),
            "account_agent_last_error": self.remote_delegate.last_error,
            "agent_last_error": self.agent.last_error,
            "session_store_backend": self.session_store.provider,
        }

    def route_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        genesys_conversation_id = str(payload.get("genesys_conversation_id") or "").strip()
        gecx_session_id = str(payload.get("gecx_session_id") or "").strip()
        request_type = str(payload.get("request_type") or "").strip()
        member_eid = str(payload.get("member_eid") or "").strip()
        intent = str(payload.get("intent") or "").strip()
        delivery_type = str(payload.get("delivery_type") or "").strip().lower()
        provided_aie_session_id = str(payload.get("aie_session_id") or "").strip()

        if request_type == "validate_account":
            if not genesys_conversation_id or not gecx_session_id:
                failure = {
                    "genesys_conversation_id": genesys_conversation_id,
                    "gecx_session_id": gecx_session_id,
                    "aie_session_id": "",
                    "request_type": "validate_account",
                    "account_found": False,
                    "phone_available": False,
                    "phone_type_mobile": False,
                    "email_available": False,
                    "account_status": "Disabled",
                    "request_status": "Failure",
                    "request_status_msg": "genesys_conversation_id and gecx_session_id are required",
                }
                return failure

            existing_session = None
            if provided_aie_session_id:
                existing_session = self.session_store.load(gecx_session_id, provided_aie_session_id)

            aie_session_id = (
                existing_session.get("aie_session_id")
                if isinstance(existing_session, dict) and existing_session.get("active", False)
                else provided_aie_session_id or str(uuid.uuid4())
            )

            account_result: dict[str, Any] = {}

            @tool
            def account_management(
                genesys_conversation_id: str,
                gecx_session_id: str,
                aie_session_id: str,
                request_type: str,
                member_eid: str = "",
                delivery_type: str = "",
                intent: str = "",
            ) -> str:
                """Delegate account-management request_type flows to acct-mgmt-agent runtime."""
                result = self.remote_delegate.invoke(
                    {
                        "genesys_conversation_id": genesys_conversation_id,
                        "gecx_session_id": gecx_session_id,
                        "aie_session_id": aie_session_id,
                        "request_type": request_type,
                        "member_eid": member_eid,
                        "delivery_type": delivery_type,
                        "intent": intent,
                    }
                )
                account_result.update(result)
                return json.dumps(result, default=str)

            prompt = (
                f"Use account_management exactly once. "
                f"genesys_conversation_id={genesys_conversation_id}, "
                f"gecx_session_id={gecx_session_id}, "
                f"aie_session_id={aie_session_id}, "
                "request_type=validate_account, "
                f"member_eid={member_eid}, intent={intent}."
            )
            self.agent.ask_with_tools(prompt, tools=[account_management], fallback="delegation failed")
            delegated = account_result
            if isinstance(delegated, dict) and delegated.get("request_type") == "validate_account":
                session_state = {
                    "genesys_conversation_id": genesys_conversation_id,
                    "gecx_session_id": gecx_session_id,
                    "aie_session_id": aie_session_id,
                    "member_eid": member_eid,
                    "intent": intent,
                    "active": True,
                    "created_at": (existing_session or {}).get("created_at") or datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "last_request_type": "validate_account",
                    "account_status": delegated.get("account_status", ""),
                    "account_found": bool(delegated.get("account_found", False)),
                }
                self.session_store.save(gecx_session_id, aie_session_id, session_state)
                return delegated

            failure = {
                "genesys_conversation_id": genesys_conversation_id,
                "gecx_session_id": gecx_session_id,
                "aie_session_id": aie_session_id,
                "request_type": "validate_account",
                "account_found": False,
                "phone_available": False,
                "phone_type_mobile": False,
                "email_available": False,
                "account_status": "Disabled",
                "request_status": "Failure",
                "request_status_msg": delegated.get("invoke_error") or "Account-management agent call failed",
            }
            return failure

        if request_type == "pw_send_link":
            if not provided_aie_session_id:
                return {
                    "genesys_conversation_id": genesys_conversation_id,
                    "gecx_session_id": gecx_session_id,
                    "aie_session_id": "",
                    "request_type": "pw_send_link",
                    "pw_link_sent": False,
                    "request_status": "Failure",
                    "request_status_msg": "aie_session_id is required",
                }

            session = self.session_store.load(gecx_session_id, provided_aie_session_id)
            if not session or not session.get("active", False):
                failure = {
                    "genesys_conversation_id": genesys_conversation_id,
                    "gecx_session_id": gecx_session_id,
                    "aie_session_id": provided_aie_session_id,
                    "request_type": "pw_send_link",
                    "pw_link_sent": False,
                    "request_status": "Failure",
                    "request_status_msg": "No active session. Call validate_account first.",
                }
                return failure

            account_result: dict[str, Any] = {}

            @tool
            def account_management(
                genesys_conversation_id: str,
                gecx_session_id: str,
                aie_session_id: str,
                request_type: str,
                member_eid: str = "",
                delivery_type: str = "",
                intent: str = "",
            ) -> str:
                """Delegate account-management request_type flows to acct-mgmt-agent runtime."""
                result = self.remote_delegate.invoke(
                    {
                        "genesys_conversation_id": genesys_conversation_id,
                        "gecx_session_id": gecx_session_id,
                        "aie_session_id": aie_session_id,
                        "request_type": request_type,
                        "member_eid": member_eid,
                        "delivery_type": delivery_type,
                        "intent": intent,
                    }
                )
                account_result.update(result)
                return json.dumps(result, default=str)

            resolved_genesys = genesys_conversation_id or session.get("genesys_conversation_id", "")
            resolved_gecx = gecx_session_id or session.get("gecx_session_id", "")
            resolved_member_eid = member_eid or session.get("member_eid", "")
            resolved_intent = intent or session.get("intent", "")
            prompt = (
                f"Use account_management exactly once. "
                f"genesys_conversation_id={resolved_genesys}, "
                f"gecx_session_id={resolved_gecx}, "
                f"aie_session_id={session['aie_session_id']}, "
                "request_type=pw_send_link, "
                f"member_eid={resolved_member_eid}, "
                f"delivery_type={delivery_type}, "
                f"intent={resolved_intent}."
            )
            self.agent.ask_with_tools(prompt, tools=[account_management], fallback="delegation failed")
            delegated = account_result
            if isinstance(delegated, dict) and delegated.get("request_type") == "pw_send_link":
                session["updated_at"] = datetime.now(timezone.utc).isoformat()
                session["last_request_type"] = "pw_send_link"
                self.session_store.save(resolved_gecx, session["aie_session_id"], session)
                return delegated

            failure = {
                "genesys_conversation_id": genesys_conversation_id or session.get("genesys_conversation_id", ""),
                "gecx_session_id": gecx_session_id or session.get("gecx_session_id", ""),
                "aie_session_id": session["aie_session_id"],
                "request_type": "pw_send_link",
                "pw_link_sent": False,
                "request_status": "Failure",
                "request_status_msg": delegated.get("invoke_error") or "Account-management agent call failed",
            }
            return failure

        if request_type == "end_session":
            if not provided_aie_session_id:
                return {
                    "genesys_conversation_id": genesys_conversation_id,
                    "gecx_session_id": gecx_session_id,
                    "aie_session_id": "",
                    "request_status": "Failure",
                    "request_status_msg": "aie_session_id is required",
                }

            session = self.session_store.load(gecx_session_id, provided_aie_session_id)
            if not session:
                return {
                    "genesys_conversation_id": genesys_conversation_id,
                    "gecx_session_id": gecx_session_id,
                    "aie_session_id": provided_aie_session_id,
                    "request_status": "Failure",
                    "request_status_msg": "Session not found",
                }

            session["active"] = False
            session["updated_at"] = datetime.now(timezone.utc).isoformat()
            session["last_request_type"] = "end_session"
            self.session_store.save(gecx_session_id, session["aie_session_id"], session)
            response = {
                "genesys_conversation_id": genesys_conversation_id or session.get("genesys_conversation_id", ""),
                "gecx_session_id": gecx_session_id or session.get("gecx_session_id", ""),
                "aie_session_id": session.get("aie_session_id", ""),
                "request_status": "Success",
                "request_status_msg": "Session Ended successfully",
            }
            return response

        unsupported = {
            "genesys_conversation_id": genesys_conversation_id,
            "gecx_session_id": gecx_session_id,
            "aie_session_id": provided_aie_session_id,
            "request_type": request_type,
            "request_status": "Failure",
            "request_status_msg": f"Unsupported request_type: {request_type or 'missing'}",
        }
        return unsupported
