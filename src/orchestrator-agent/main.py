from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
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
def orchestrate(payload: RequestEnvelope) -> dict:
    return SERVICE.route_request(payload.model_dump())


@app.post("/clear-memory")
def clear_memory() -> dict[str, str]:
    empty = json.dumps({}, indent=2)
    base = Path(__file__).parent
    targets = [
        base / "data" / "orchestrator-memory.json",
        base.parent / "acct-mgmt-agent" / "data" / "account-memory.json",
    ]
    cleared = []
    for path in targets:
        if path.exists():
            path.write_text(empty)
            cleared.append(path.name)
    return {"status": "ok", "cleared": ", ".join(cleared)}
