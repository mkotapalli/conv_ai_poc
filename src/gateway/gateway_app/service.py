from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boto3
import jwt
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from jwt import PyJWKClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.getenv("CONFIG_FILE", SERVICE_ROOT / "config" / "application.properties"))


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


class GatewayService:
    def __init__(self) -> None:
        self.settings = Settings(CONFIG_PATH)

    def health(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.settings.get("app.name", "gateway"),
            "target": self.settings.get("service.orchestrator.url", "not-configured"),
        }

    def verify_okta_token(self, token: str) -> dict[str, Any]:
        if not token and self.settings.get_bool("security.okta.enabled", False):
            raise ValueError("Missing bearer token")

        if not self.settings.get_bool("security.okta.enabled", False):
            return {
                "sub": self.settings.get("security.local.user", "local-dev-user"),
                "auth_mode": "okta-bypass",
            }

        jwks_url = self.settings.get("security.okta.jwks_url")
        issuer = self.settings.get("security.okta.issuer")
        audience = self.settings.get("security.okta.audience")

        if not all([jwks_url, issuer, audience]):
            raise ValueError("Okta settings are incomplete in application.properties")

        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
        )

    def _signed_post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = self.settings.get_int("http.timeout.seconds", 20)
        region = self.settings.get("aws.region", "us-east-1")
        service_name = self.settings.get("auth.sigv4.service", "execute-api")
        request_body = json.dumps(payload)
        headers: dict[str, str] = {"content-type": "application/json"}

        if self.settings.get_bool("auth.sigv4.enabled", True):
            session = boto3.Session(region_name=region)
            credentials = session.get_credentials()
            if credentials is not None:
                request = AWSRequest(method="POST", url=url, data=request_body, headers=headers)
                SigV4Auth(credentials.get_frozen_credentials(), service_name, region).add_auth(request)
                headers = dict(request.headers.items())
            elif not self.settings.get_bool("development.allow_unsigned_local", True):
                raise RuntimeError("SigV4 signing is enabled but no AWS credentials were found.")

        response = requests.post(url, data=request_body, headers=headers, timeout=timeout_seconds)
        response.raise_for_status()
        return response.json()

    def forward_to_orchestrator(self, payload: dict[str, Any], user_context: dict[str, Any]) -> dict[str, Any]:
        request_body = {
            "conversation_id": payload.get("conversation_id"),
            "message": payload.get("message", ""),
            "user_context": user_context,
        }
        orchestrator_url = self.settings.get("service.orchestrator.url")

        if not orchestrator_url:
            return {
                "status": "error",
                "message": "The orchestrator URL is missing from the gateway properties file.",
            }

        try:
            return self._signed_post(orchestrator_url, request_body)
        except Exception as exc:  # pragma: no cover - depends on network/AWS env
            return {
                "status": "degraded",
                "gateway": self.settings.get("app.name", "gateway"),
                "message": str(payload.get("message", "")),
                "answer": (
                    "Gateway authentication succeeded, but the orchestrator call could not be completed. "
                    f"This local POC fallback captured: {exc}"
                ),
                "user_context": user_context,
            }
