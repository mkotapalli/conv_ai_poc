from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

SERVICE_ROOT = Path(__file__).resolve().parent
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
        self.properties = load_properties(path)

    @staticmethod
    def _to_env_key(key: str) -> str:
        return key.upper().replace(".", "_").replace("-", "_")

    def get(self, key: str, default: str = "") -> str:
        env_key = self._to_env_key(key)
        return os.getenv(env_key, self.properties.get(key, default))


class AccountApiMockStore:
    def __init__(self, source_path: str) -> None:
        path = Path(source_path)
        if not path.is_absolute():
            path = SERVICE_ROOT / path
        self.source_path = path

    @staticmethod
    def _normalize_intent(raw_intent: str) -> str:
        value = raw_intent.strip().lower().replace(" ", "_")
        if value in {"account_unlock", "unlock", "password_unlock", "unlock_account"}:
            return "Account_Unlock"
        if value in {"account_pw_reset", "account_reset", "password_reset", "reset", "reset_password"}:
            return "Account_PW_Reset"
        return "General"

    def _rows(self) -> list[dict[str, str]]:
        if not self.source_path.exists():
            return []
        payload = json.loads(self.source_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [{str(k): "" if v is None else str(v) for k, v in row.items()} for row in payload if isinstance(row, dict)]
        return []

    @staticmethod
    def _intent_to_action(normalized_intent: str) -> str:
        return {"Account_Unlock": "unlock_account", "Account_PW_Reset": "reset_password"}.get(
            normalized_intent, ""
        )

    def lookup(self, intent: str, contract_number: str) -> dict[str, Any]:
        normalized_intent = self._normalize_intent(intent)
        normalized_contract = contract_number.strip()
        for row in self._rows():
            row_intent = self._normalize_intent(row.get("intent", ""))
            row_contract = row.get("contractNumber", "").strip()
            if row_intent == normalized_intent and row_contract == normalized_contract:
                return {
                    "found": True,
                    "intent": normalized_intent,
                    "contractNumber": row_contract,
                    "action": row.get("action", "") or self._intent_to_action(normalized_intent),
                    "status": row.get("status", "success") or "success",
                    "failure_reason": row.get("failure_reason", "") or "",
                }

        return {
            "found": False,
            "intent": normalized_intent,
            "contractNumber": normalized_contract,
            "action": self._intent_to_action(normalized_intent),
            "status": "failure",
            "failure_reason": "account not found",
        }


SETTINGS = Settings(CONFIG_PATH)
STORE = AccountApiMockStore(SETTINGS.get("mock.data.source_path", "data/account_api_mock.json"))

MCP_SERVER = FastMCP(
    name=SETTINGS.get("app.name", "account-api-mcp"),
    instructions=(
        "Mock account management API via MCP. "
        "Use account_api_lookup to return data-driven responses from CSV or JSON."
    ),
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
)


@MCP_SERVER.tool(
    name="account_api_lookup",
    description=(
        "Lookup mock account API response by intent and contract_number. "
        "Supported intents include Account_Unlock and Account_PW_Reset."
    ),
)
def account_api_lookup(intent: str, contract_number: str) -> str:
    return json.dumps(STORE.lookup(intent=intent, contract_number=contract_number))


@MCP_SERVER.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": SETTINGS.get("app.name", "account-api-mcp"),
            "source_path": str(STORE.source_path),
            "mcp_path": "/mcp",
            "health_path": "/health",
        }
    )


app = MCP_SERVER.streamable_http_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", os.getenv("SERVER_PORT", SETTINGS.get("server.port", "8000"))))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
