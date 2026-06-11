from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from orchestrator_agent.service import OrchestratorService

SERVICE = OrchestratorService()
app = FastAPI(title="Orchestrator Agent", version="0.1.0")

class RequestEnvelope(BaseModel):
    genesys_conversation_id: str | None = None
    gecx_session_id: str | None = None
    aie_session_id: str | None = None
    request_type: str = Field(min_length=1)
    member_eid: str | None = None
    delivery_type: str | None = None
    intent: str | None = None


RequestEnvelope.model_rebuild()


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "orchestrator-agent",
        "message": "Use POST /orchestrate for intent classification and routing.",
    }


@app.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    return SERVICE.health()


@app.post("/orchestrate")
def orchestrate(payload: RequestEnvelope) -> dict:
    return SERVICE.route_request(payload.model_dump())


@app.post("/invocations")
def invocations(payload: RequestEnvelope) -> dict:
    return SERVICE.route_request(payload.model_dump())
