from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator_agent.service import OrchestratorService

SERVICE = OrchestratorService()
app = FastAPI(title="Orchestrator Agent", version="0.1.0")

class RequestEnvelope(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1)
    user_context: dict[str, Any] = Field(default_factory=dict)


RequestEnvelope.model_rebuild()


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "orchestrator-agent",
        "message": "Use POST /orchestrate for intent classification and routing.",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return SERVICE.health()


@app.post("/orchestrate")
def orchestrate(payload: RequestEnvelope, request: Request) -> dict:
    try:
        SERVICE.validate_sigv4(request.headers)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    return SERVICE.route_request(payload.model_dump())
