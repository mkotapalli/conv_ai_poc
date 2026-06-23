from __future__ import annotations

import json
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import requests
from bedrock_agentcore.memory.constants import ConversationalMessage, MessageRole
from bedrock_agentcore.memory.session import MemorySessionManager
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

    def _agentcore_session(self, session_id: str):
        if not self._manager:
            raise RuntimeError("AgentCore memory manager is not configured")
        return self._manager.create_memory_session(actor_id=session_id, session_id=session_id)

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

    def load(self, aie_session_id: str) -> dict[str, Any] | None:
        if self.uses_agentcore and self._manager:
            try:
                events = self._manager.list_events(
                    actor_id=aie_session_id,
                    session_id=aie_session_id,
                    max_results=100,
                    include_payload=True,
                )
                for event in reversed(events):
                    state = self._extract_state_from_event(event)
                    if state is not None:
                        return state
            except Exception as exc:
                LOGGER.exception("Failed to load session state from AgentCore Memory: %s", exc)
                return None

        return self._memory.get(aie_session_id)

    def save(self, aie_session_id: str, state: dict[str, Any]) -> None:
        if self.uses_agentcore and self._manager:
            try:
                session = self._agentcore_session(aie_session_id)
                payload = f"{self.STATE_PREFIX}{json.dumps(state, sort_keys=True)}"
                session.add_turns([ConversationalMessage(payload, MessageRole.OTHER)])
                return
            except Exception as exc:
                LOGGER.exception("Failed to persist session state to AgentCore Memory: %s", exc)
                return

        self._memory[aie_session_id] = state
        self._save()


class AccountApiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error = ""

    def _endpoint(self, key: str) -> str:
        return self.settings.get(key, "").strip()

    def _call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not endpoint:
            return None

        timeout_seconds = self.settings.get_int("http.timeout.seconds", 20)
        try:
            response = requests.post(endpoint, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict):
                self.last_error = ""
                return result
            return {"raw_response": result}
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            LOGGER.exception("API call failed for %s", endpoint)
            return None

    def call_account_validation_api(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self._call(self._endpoint("service.api.account_validation_url"), payload)

    def call_send_link_api(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self._call(self._endpoint("service.api.send_link_url"), payload)


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
        self.session_store = SessionStateStore(
            storage_path=self.settings.get("session.storage_path", "data/account-sessions.json"),
            provider=self.settings.get("memory.provider", "local"),
            memory_id=self.settings.get("memory.agentcore.memory_id", ""),
            region=self.settings.get("aws.region", "us-east-1"),
        )
        self.api_client = AccountApiClient(self.settings)
        self._rng = random.Random()
        random_seed = self.settings.get("service.stub.random_seed", "").strip()
        if random_seed:
            self._rng.seed(random_seed)
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
            "session_store": self.settings.get("session.storage_path", "data/account-sessions.json"),
            "session_store_backend": self.session_store.provider,
            "account_validation_api_url": self.settings.get("service.api.account_validation_url", ""),
            "send_link_api_url": self.settings.get("service.api.send_link_url", ""),
            "a2a_enabled": str(self.settings.get_bool("service.a2a.enabled", True)).lower(),
            "a2a_agent_ready": str(self.get_a2a_agent() is not None).lower(),
            "agent_last_error": self.agent.last_error,
            "api_last_error": self.api_client.last_error,
        }

    def _pick_stub_success(self) -> bool:
        return self._rng.random() >= 0.5

    def _stub_member_channels(self, member_eid: str, *, account_found: bool) -> dict[str, Any]:
        last_digit = int(member_eid[-1]) if member_eid and member_eid[-1].isdigit() else 0
        account_status = "Locked" if account_found and last_digit in {5, 7, 9} else "Enabled"
        channels = {
            "account_found": account_found,
            "phone_available": account_found and last_digit % 3 != 1,
            "phone_type_mobile": account_found and last_digit % 2 == 0,
            "email_available": account_found and last_digit % 2 == 1,
            "account_status": account_status,
        }
        if member_eid:
            self._member_channel_map[member_eid] = channels
        return channels

    def _create_session(self, *, genesys_conversation_id: str, gecx_session_id: str, member_eid: str, intent: str, account_status: str, account_found: bool) -> str:
        session_id = str(uuid.uuid4())
        self.session_store.save(
            session_id,
            {
                "genesys_conversation_id": genesys_conversation_id,
                "gecx_session_id": gecx_session_id,
                "aie_session_id": session_id,
                "member_eid": member_eid,
                "intent": intent,
                "account_status": account_status,
                "account_found": account_found,
                "active": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "last_request_type": "validate_account",
            },
        )
        return session_id

    def _build_validation_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        genesys_conversation_id = str(payload.get("genesys_conversation_id") or "").strip()
        gecx_session_id = str(payload.get("gecx_session_id") or "").strip()
        member_eid = str(payload.get("member_eid") or "").strip()
        intent = str(payload.get("intent") or "").strip()

        api_result = self.api_client.call_account_validation_api(payload)
        if isinstance(api_result, dict):
            aie_session_id = str(api_result.get("aie_session_id") or "").strip() or self._create_session(
                genesys_conversation_id=genesys_conversation_id,
                gecx_session_id=gecx_session_id,
                member_eid=member_eid,
                intent=intent,
                account_status=str(api_result.get("account_status", "")),
                account_found=bool(api_result.get("account_found", True)),
            )
            request_status = str(api_result.get("request_status") or "Success").strip().lower()
            # Persist the session even when the upstream API generates the session id,
            # so pw_send_link can resolve the same aie_session_id on the next call.
            self.session_store.save(
                aie_session_id,
                {
                    "genesys_conversation_id": genesys_conversation_id,
                    "gecx_session_id": gecx_session_id,
                    "aie_session_id": aie_session_id,
                    "member_eid": member_eid,
                    "intent": intent,
                    "account_status": str(api_result.get("account_status", "")),
                    "account_found": bool(api_result.get("account_found", request_status == "success")),
                    "active": request_status == "success",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "last_request_type": "validate_account",
                },
            )
            api_result.setdefault("genesys_conversation_id", genesys_conversation_id)
            api_result.setdefault("gecx_session_id", gecx_session_id)
            api_result.setdefault("aie_session_id", aie_session_id)
            api_result.setdefault("request_type", "validate_account")
            api_result.setdefault("request_status", "Success")
            api_result.setdefault("request_status_msg", "Account validated")
            return api_result

        success = self._pick_stub_success()
        channels = self._stub_member_channels(member_eid, account_found=success)
        aie_session_id = self._create_session(
            genesys_conversation_id=genesys_conversation_id,
            gecx_session_id=gecx_session_id,
            member_eid=member_eid,
            intent=intent,
            account_status=channels["account_status"],
            account_found=channels["account_found"],
        )
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
            "request_status": "Success" if success else "Failure",
            "request_status_msg": "Account validated" if success else "Member account not found",
            "response_source": "stub",
        }
        return response

    def _build_send_link_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        genesys_conversation_id = str(payload.get("genesys_conversation_id") or "").strip()
        gecx_session_id = str(payload.get("gecx_session_id") or "").strip()
        aie_session_id = str(payload.get("aie_session_id") or "").strip()
        member_eid = str(payload.get("member_eid") or "").strip()
        delivery_type = str(payload.get("delivery_type") or "").strip().lower()

        if delivery_type not in {"sms", "email"}:
            return {
                "genesys_conversation_id": genesys_conversation_id,
                "gecx_session_id": gecx_session_id,
                "aie_session_id": aie_session_id,
                "request_type": "pw_send_link",
                "pw_link_sent": False,
                "request_status": "Failure",
                "request_status_msg": "Unsupported delivery_type. Use sms or email.",
                "response_source": "stub",
            }

        session = self.session_store.load(aie_session_id) if aie_session_id else None
        if not session or not session.get("active", False):
            return {
                "genesys_conversation_id": genesys_conversation_id,
                "gecx_session_id": gecx_session_id,
                "aie_session_id": aie_session_id,
                "request_type": "pw_send_link",
                "pw_link_sent": False,
                "request_status": "Failure",
                "request_status_msg": "No active validation session. Call validate_account first.",
                "response_source": "stub",
            }

        api_result = self.api_client.call_send_link_api(payload)
        if isinstance(api_result, dict):
            api_result.setdefault("genesys_conversation_id", genesys_conversation_id)
            api_result.setdefault("gecx_session_id", gecx_session_id)
            api_result.setdefault("aie_session_id", aie_session_id)
            api_result.setdefault("request_type", "pw_send_link")
            api_result.setdefault("request_status", "Success")
            api_result.setdefault("request_status_msg", "Password reset link sent")
            return api_result

        success = self._pick_stub_success()
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        session["last_request_type"] = "pw_send_link"
        session["last_delivery_type"] = delivery_type
        session["pw_link_sent"] = success
        session["member_eid"] = member_eid or session.get("member_eid", "")
        self.session_store.save(aie_session_id, session)
        return {
            "genesys_conversation_id": genesys_conversation_id,
            "gecx_session_id": gecx_session_id,
            "aie_session_id": aie_session_id,
            "request_type": "pw_send_link",
            "pw_link_sent": success,
            "request_status": "Success" if success else "Failure",
            "request_status_msg": "Password reset link sent" if success else f"{delivery_type} delivery channel not available",
            "response_source": "stub",
        }

    def _build_end_session_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        genesys_conversation_id = str(payload.get("genesys_conversation_id") or "").strip()
        gecx_session_id = str(payload.get("gecx_session_id") or "").strip()
        aie_session_id = str(payload.get("aie_session_id") or "").strip()

        session = self.session_store.load(aie_session_id) if aie_session_id else None
        if not session:
            return {
                "genesys_conversation_id": genesys_conversation_id,
                "gecx_session_id": gecx_session_id,
                "aie_session_id": aie_session_id,
                "request_type": "end_session",
                "request_status": "Failure",
                "request_status_msg": "Session not found",
            }

        session["active"] = False
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        session["last_request_type"] = "end_session"
        self.session_store.save(aie_session_id, session)
        return {
            "genesys_conversation_id": genesys_conversation_id or session.get("genesys_conversation_id", ""),
            "gecx_session_id": gecx_session_id or session.get("gecx_session_id", ""),
            "aie_session_id": aie_session_id,
            "request_status": "Success",
            "request_status_msg": "Session Ended successfully",
        }

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
            response = self._build_validation_response(payload)
            self.memory.append(conversation_key, "assistant", json.dumps(response, default=str))
            return response

        if request_type == "pw_send_link":
            response = self._build_send_link_response(payload)
            self.memory.append(conversation_key, "assistant", json.dumps(response, default=str))
            return response

        if request_type == "end_session":
            response = self._build_end_session_response(payload)
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
