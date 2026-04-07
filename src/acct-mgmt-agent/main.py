from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from acct_mgmt_agent.service import AccountManagementService

SERVICE = AccountManagementService()
app = FastAPI(title="Account Management Agent", version="0.1.0")


class AgentRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1)
    intent: str = Field(default="general")
    user_context: dict[str, Any] = Field(default_factory=dict)


AgentRequest.model_rebuild()


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "acct-mgmt-agent",
        "message": "Use POST /assist for password reset and unlock responses.",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return SERVICE.health()


@app.post("/assist")
def assist(payload: AgentRequest, request: Request) -> dict:
    try:
        SERVICE.validate_sigv4(request.headers)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    return SERVICE.handle_request(payload.model_dump())
