from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent


def load_module(module_name: str, file_path: Path):
    service_dir = str(file_path.parent)
    if service_dir not in sys.path:
        sys.path.insert(0, service_dir)
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


orchestrator_module = load_module("orchestrator_main", ROOT / "src" / "orchestrator-agent" / "main.py")
account_module = load_module("acct_mgmt_main", ROOT / "src" / "acct-mgmt-agent" / "main.py")

orchestrator_client = TestClient(orchestrator_module.app)
account_client = TestClient(account_module.app)


def fake_orchestrator_call(payload, user_context):
    response = orchestrator_client.post(
        "/orchestrate",
        json={**payload, "user_context": user_context},
    )
    response.raise_for_status()
    return response.json()


def fake_account_call(payload):
    response = account_client.post(
        "/assist",
        json=payload,
    )
    response.raise_for_status()
    return response.json()


orchestrator_module.SERVICE.call_account_agent = fake_account_call

response = orchestrator_client.post(
    "/orchestrate",
    json={
        "genesys_conversation_id": "7a833b7d-5747-407a-a9c9-5781aea9539f",
        "gecx_session_id": "e97b4552-a8bc-4c0a-a3d1-5029c1a5f217",
        "request_type": "validate_account",
        "member_eid": "70400040700130465465",
        "intent": "Account_Unlock_PW_Reset",
    },
)

print(f"status={response.status_code}")
print(json.dumps(response.json(), indent=2))

if response.status_code != 200:
    raise SystemExit(1)
