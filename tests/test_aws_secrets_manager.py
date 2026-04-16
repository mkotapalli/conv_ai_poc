from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "orchestrator-agent"))

from orchestrator_agent import service as orchestrator_service


class _FakeSecretsClient:
    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        assert SecretId == "bcbs-dev-convai-secrets"

        secret_payload = {
            "AWS_ACCESS_KEY_ID": "secret-access-key-id",
            "AWS_SECRET_ACCESS_KEY": "secret-access-key",
            "AWS_SESSION_TOKEN": "secret-session-token",
        }
        print(json.dumps(secret_payload))

        return {"SecretString": json.dumps(secret_payload)}


class _FakeSession:
    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs

    def client(self, service_name: str, region_name: str | None = None):
        assert service_name == "secretsmanager"
        assert region_name == "us-east-1"
        return _FakeSecretsClient()


def test_settings_load_aws_credentials_from_secrets_manager(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "application.properties"
    config_path.write_text(
        "\n".join(
            [
                "aws.region=us-east-1",
                "aws.secretsmanager.enabled=true",
                "aws.secretsmanager.secret_name=bcbs-dev-convai-secrets",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(orchestrator_service.boto3.session, "Session", _FakeSession)

    orchestrator_service.Settings(config_path)

    assert orchestrator_service.os.environ["AWS_ACCESS_KEY_ID"] == "secret-access-key-id"
    assert orchestrator_service.os.environ["AWS_SECRET_ACCESS_KEY"] == "secret-access-key"
    assert orchestrator_service.os.environ["AWS_SESSION_TOKEN"] == "secret-session-token"


def test_orchestrator_prefers_inference_profile_id(tmp_path, monkeypatch) -> None:
    sys.path.insert(0, str(REPO_ROOT / "src" / "orchestrator-agent"))
    from orchestrator_agent import service as orchestrator_service

    config_path = tmp_path / "orchestrator.properties"
    config_path.write_text(
        "\n".join(
            [
                "aws.region=us-east-1",
                "bedrock.model_id=legacy-model-id",
                "bedrock.inference_profile_id=us.amazon.nova-lite-v1:0",
                "aws.secretsmanager.enabled=false",
            ]
        ),
        encoding="utf-8",
    )

    captured: dict[str, str] = {}

    class _FakeBedrockModel:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    class _FakeAgent:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(orchestrator_service, "BedrockModel", _FakeBedrockModel)
    monkeypatch.setattr(orchestrator_service, "Agent", _FakeAgent)

    settings = orchestrator_service.Settings(config_path)
    responder = orchestrator_service.StrandsResponder(settings, "system prompt", "test-agent")

    responder.create_agent(force_new=True)

    assert captured["model_id"] == "us.amazon.nova-lite-v1:0"
    assert captured["region_name"] == "us-east-1"
